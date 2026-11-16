#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generate the block that brings deploy/db-init/schema.sql back in step with the model.

`deploy/db-init/schema.sql` is what a self-hosted instance is built from: `deploy/compose.yaml`
runs it through psql on every `compose up`. It started life as an EF idempotent migration bundle,
but EF migrations are git-ignored here, so nobody could regenerate it — it was kept up by appending
blocks by hand, and it fell behind. By #318 a fresh instance was missing 167 columns across 29
tables, including whole feature tables, and the application could not have run against it.

This closes the gap without touching the EF history guards, which is what keeps existing instances
safe: appending a new migration id would make every guarded block believe it had never run and try
to create everything a second time. The block emitted here is plain idempotent DDL instead —
CREATE TABLE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS, type changes guarded on the current type — so
it is a no-op on an instance that already has the objects, whatever its migration history says.

    python3 Scripts/gen_db_init_delta.py            print the block
    python3 Scripts/gen_db_init_delta.py --append   append it to deploy/db-init/schema.sql
    python3 Scripts/gen_db_init_delta.py --check    exit 1 if a delta exists (for CI or a pre-PR check)
    python3 Scripts/gen_db_init_delta.py --append --no-obsolete
                                                    ... without another branch's DROP statements

How it works: build a throwaway database from the committed schema.sql, compare it against the
development database, and emit the difference. The comparison is therefore against what a fresh
install *actually* gets, not against what anyone believes it gets.

Views, functions and triggers are not part of the emitted block — `deploy/compose.yaml` replays
those separately from `Scripts/dump/PostgreSQL/*/{Functions,Views,Triggers}`. They are still
*counted* here, because being replayed from a file is worth nothing when there is no file: two
search functions and the BMI trigger lived in the development database and in no dump for months,
and every check said "in step" because a schema comparison cannot see a routine. `--check` now
fails on that too, and names the command that fixes it.

Seed rows are, since #393. They come from the *model* (Scripts/csharp/model_seed_data.cs), never
from a database: the development database holds the same tables with rows someone edited, rows
someone added, and the developer's own diaries beside the diary templates. EF writes HasData rows
through a migration and migrations are git-ignored here, so a seed added to a configuration reached
the development database and no self-hosted instance at all.

Backfills are the one thing here nobody can derive. Everything above is the difference between two
schemas, and a data migration is not a difference between two schemas: adding a column is one and
*filling* it from a lookup list is not. Left to this file alone, a self-hosted instance received the
new column empty forever while the development database had it filled - and `--check` reported "in
step" throughout, because the schemas did agree. Write those statements into a file under
Scripts/sql/backfill/ and they are carried into schema.sql verbatim, once each. See the README there
for the two rules they have to obey.
"""
import argparse
import datetime
import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from project import project

SCRIPTS = str(project.root / "Scripts")
REPO = str(project.root)
SCHEMA_FILE = str(project.path("db_init.schema_file", "deploy/db-init/schema.sql"))
DUMP_ROOT = str(project.path("db_init.routines", "Scripts/dump/PostgreSQL"))
SCRATCH = project.get("db_init.scratch_database",
                      f"{project.env_prefix.lower()}_schema_check")

MARKER = "-- === db-init delta"

# Columns that exist on the development database but are not part of the model. They must not reach
# a fresh instance: shipping them would turn local drift into the shape everyone gets.
#
# Empty since #316 — loinccode and closure were the last two, and they are mapped now rather than
# excluded. Kept because the situation recurs: a column added by hand against the development
# database belongs here until it is either mapped or dropped.
DRIFT_COLUMNS: set[tuple[str, str, str]] = set()

ENV = project.psql_environment()

HOST = project.env("DB_HOST", project.get("database.host", "localhost"))
PORT = str(project.env("DB_PORT", project.get("database.port", "5432")))
LIVE = project.env("DB_NAME", project.get("database.name", project.name))
USER = project.env("DB_USER", project.get("database.user", os.environ.get("USER", "postgres")))


def psql(database, arguments, check=True):
    command = ["psql", "-h", HOST, "-p", PORT, "-U", USER, "-d", database,
               "-v", "ON_ERROR_STOP=1", *arguments]
    result = subprocess.run(command, cwd=REPO, env=ENV, text=True, capture_output=True)

    if check and result.returncode != 0:
        sys.stderr.write((result.stderr or result.stdout or "").strip() + "\n")
        sys.exit(result.returncode)

    return result


def rows(database, sql, check=True):
    """Query results split on the unit separator, one list of fields per row.

    Stripped of newlines only, never with a bare `strip()`: Python counts \\x1f as whitespace, so
    that would eat the separator that ends the last row - and with it the last row's final field
    whenever it is empty. Exactly one row came back a field short, the last one, which is the kind of
    off-by-one that reads as a database quirk for an hour.
    """
    result = psql(database, ["-tAF", "\x1f", "-c", sql], check=check)

    return [line.split("\x1f") for line in result.stdout.strip("\n").splitlines() if line]


# pg_dump names a trigger by its table and its own name, so the database side has to spell it the
# same way for the two to be comparable at all.
ROUTINE_INVENTORY = (
    "select case p.prokind when 'p' then 'PROCEDURE' else 'FUNCTION' end, n.nspname, p.proname "
    "from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
    "where n.nspname not in ('pg_catalog','information_schema') "
    "union all "
    "select 'VIEW', schemaname, viewname from pg_views "
    "where schemaname not in ('pg_catalog','information_schema') "
    "union all "
    "select 'TRIGGER', n.nspname, c.relname || ' ' || t.tgname "
    "from pg_trigger t join pg_class c on c.oid = t.tgrelid "
    "join pg_namespace n on n.oid = c.relnamespace where not t.tgisinternal")

# The header pg_dump writes above every object, which dump_db.sh keeps as the second line of each
# file it splits out.
DUMP_HEADER = re.compile(
    r"^-- Name: (?P<name>.+?); Type: (?P<type>[A-Z ]+); Schema: (?P<schema>[^;\s]+)")


def undumped_routines():
    """Routines the database has and `Scripts/dump/PostgreSQL` does not.

    Matched on schema, kind and bare name rather than on the full signature: the file name pg_dump
    derives from an argument list is its own small dialect, and the question here is only whether
    anything was dumped for this routine at all. Comparing signatures is verify_db_init.py's job,
    which has a fresh database to compare against and can ask PostgreSQL instead of a file name.
    """
    dumped = set()

    for directory in ("Functions", "Views", "Triggers"):
        for path in glob.glob(f"{DUMP_ROOT}/*/{directory}/*.sql"):
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                found = DUMP_HEADER.match(line)

                if found:
                    name = found["name"].split("(")[0].strip()
                    dumped.add((found["type"].strip(), found["schema"], name))

    live = {(kind, schema, name) for kind, schema, name in rows(LIVE, ROUTINE_INVENTORY)}

    return sorted(live - dumped)


def build_scratch():
    """Rebuild the throwaway database from the committed schema.sql."""
    psql("postgres", ["-c", f'DROP DATABASE IF EXISTS {SCRATCH}'])
    psql("postgres", ["-c", f'CREATE DATABASE {SCRATCH}'])
    # No -1: the bundle opens its own transactions, and an outer one would be closed by the first
    # inner COMMIT, leaving the rest to run unprotected.
    psql(SCRATCH, ["-f", SCHEMA_FILE])


COLUMN_QUERY = """
select table_schema, table_name, column_name,
       case when data_type = 'ARRAY' then substr(udt_name, 2) || '[]' else data_type end,
       coalesce(character_maximum_length::text, ''), is_nullable,
       coalesce(column_default, '')
from information_schema.columns c
where table_schema not in ('pg_catalog', 'information_schema')
  and table_name <> '__EFMigrationsHistory'
  and exists (select 1 from information_schema.tables t
              where t.table_schema = c.table_schema and t.table_name = c.table_name
                and t.table_type = 'BASE TABLE')
order by table_schema, table_name, ordinal_position
"""

TABLE_QUERY = """
select table_schema, table_name from information_schema.tables
where table_type = 'BASE TABLE' and table_schema not in ('pg_catalog', 'information_schema')
  and table_name <> '__EFMigrationsHistory'
order by 1, 2
"""


def sql_type(data_type, length):
    if data_type == "character varying" and length:
        return f"character varying({length})"

    return data_type


def nullability_change(schema, table, column, nullable):
    """One column's NOT NULL brought in line with the model, guarded on what the instance has.

    Dropping the constraint is always safe. Setting it is not: an instance that has been running
    since before the column was required may hold rows with nothing in it, and `SET NOT NULL` would
    abort the whole file on `compose up`. Those rows are the instance owner's data and this script
    cannot invent values for them, so it stops with a sentence that says which column and why,
    instead of leaving a half-applied schema behind.
    """
    presence = (f"SELECT 1 FROM information_schema.columns "
                f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
                f"AND column_name = '{column}' AND is_nullable = ")

    if nullable:
        return (f"DO $nullability$ BEGIN IF EXISTS ({presence}'NO') THEN\n"
                f'  ALTER TABLE {schema}."{table}" ALTER COLUMN "{column}" DROP NOT NULL;\n'
                f"END IF; END $nullability$;")

    return (f"DO $nullability$ BEGIN IF EXISTS ({presence}'YES') THEN\n"
            f'  IF EXISTS (SELECT 1 FROM {schema}."{table}" WHERE "{column}" IS NULL) THEN\n'
            f"    RAISE EXCEPTION 'Refusing to make {schema}.{table}.{column} NOT NULL: rows hold "
            f"no value for it. Fill or remove those rows, then run this file again.';\n"
            f"  END IF;\n"
            f'  ALTER TABLE {schema}."{table}" ALTER COLUMN "{column}" SET NOT NULL;\n'
            f"END IF; END $nullability$;")


def default_change(schema, table, column, default):
    """One column's DEFAULT brought in line with the model.

    No guard: setting a default to what it already is, or dropping one that is not there, changes
    nothing - so the statement is idempotent on its own, unlike a type change.

    The blind spot this closes is the same one as the nullability above, and it left
    sales."Orders"."Status" defaulting to `0` on every fresh instance: converting the
    enum to text carried the old integer default across as the string "0", which is not a status any
    reader of that column would recognise.
    """
    if default:
        return f'ALTER TABLE {schema}."{table}" ALTER COLUMN "{column}" SET DEFAULT {default};'

    return f'ALTER TABLE {schema}."{table}" ALTER COLUMN "{column}" DROP DEFAULT;'


def constraint_guard(name, schema, table):
    """
    The opening of a DO block that adds a constraint only if that table does not already carry it.

    Names it by table and not by name alone. A constraint name is unique per table in Postgres, not
    per database, so two tables can both carry a `PK_SleepSessions` - which is exactly what happens
    while a table moves between schemas. Guarding on the bare name then skips the primary key of the
    new table because the old one still exists, and the foreign key pointing at it fails on the next
    run with "no unique constraint matching given keys".
    """
    return (f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint con "
            f"JOIN pg_class c ON c.oid = con.conrelid "
            f"JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE con.conname = '{name}' AND n.nspname = '{schema}' "
            f"AND c.relname = '{table}') THEN")


def table_ddl(schema, table):
    """The CREATE TABLE, indexes and constraints for one table, made re-runnable."""
    dump = subprocess.run(
        ["pg_dump", "--schema-only", "--no-owner", "--no-privileges",
         "-h", HOST, "-p", PORT, "-U", USER, "-d", LIVE, "-t", f'{schema}."{table}"'],
        cwd=REPO, env=ENV, text=True, capture_output=True)

    if dump.returncode != 0:
        sys.stderr.write(dump.stderr)
        sys.exit(dump.returncode)

    statements = []

    for raw in re.split(r";\s*\n", dump.stdout):
        # Drop comments, psql meta-commands (pg_dump 18 emits \restrict / \unrestrict) and the
        # session SET lines — none of them belong in a file replayed into an existing database.
        statement = "\n".join(
            line for line in raw.splitlines()
            if line.strip()
            and not line.strip().startswith("--")
            and not line.strip().startswith("\\")
            and not re.match(r"^(SET|SELECT)\s", line.strip()))

        if not statement.strip():
            continue

        # ALTER COLUMN ... ADD GENERATED ... AS IDENTITY fails if the column already has it.
        identity = re.match(r'ALTER TABLE (\S+)\."([^"]+)" ALTER COLUMN "([^"]+)" ADD GENERATED',
                            statement)

        if identity:
            table_schema, table_name, column = identity.groups()
            statements.append(
                f"DO $identity$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attribute a "
                f"JOIN pg_class c ON c.oid = a.attrelid "
                f"JOIN pg_namespace n ON n.oid = c.relnamespace "
                f"WHERE n.nspname = '{table_schema}' AND c.relname = '{table_name}' "
                f"AND a.attname = '{column}' AND a.attidentity <> '') THEN\n"
                f"{statement};\nEND IF; END $identity$;")
            continue

        statement = statement.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
        statement = statement.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
        statement = statement.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1)
        statement = statement.replace("CREATE SEQUENCE ", "CREATE SEQUENCE IF NOT EXISTS ", 1)

        # ALTER TABLE ... ADD CONSTRAINT has no IF NOT EXISTS; guard it on pg_constraint instead.
        match = re.search(r'ADD CONSTRAINT "([^"]+)"', statement)
        target = re.search(r'ALTER TABLE (?:ONLY )?([a-z_]+)\."([^"]+)"', statement)

        if match and target:
            statement = (constraint_guard(match.group(1), target.group(1), target.group(2))
                         + f"\n{statement};\nEND IF; END $$")

        statements.append(statement + ";")

    # Split creation from wiring. Tables are emitted alphabetically, so a foreign key can easily
    # point at a table that has not been created yet — NutrientReferenceValues before Nutrients.
    # Every table is created first, then every constraint and index.
    creation, keys, wiring = [], [], []

    for statement in statements:
        is_constraint = "ADD CONSTRAINT" in statement
        is_index = re.search(r"CREATE (UNIQUE )?INDEX", statement) is not None

        if is_constraint and re.search(r"(PRIMARY KEY|UNIQUE)", statement):
            # A foreign key can only reference a column that already carries a primary key or a
            # unique constraint, and those arrive per table — so every key goes before every
            # reference, not just before the ones on the same table.
            keys.append(statement)
        elif is_constraint or is_index:
            wiring.append(statement)
        else:
            creation.append(statement)

    return creation, keys, wiring


def build_delta(keep_obsolete=True):
    """The delta block, and the counts for its summary line.

    `keep_obsolete=False` leaves out the two destructive sections - the DROP COLUMN and DROP TABLE
    statements for objects the model no longer has. They are the only part of this block that can
    damage a fresh instance when it is generated on a branch, because the comparison is against the
    development database, which carries every branch applied to it. A column another branch dropped
    is therefore reported here as obsolete while this branch's model still declares it, and shipping
    that DROP would take the column away from an instance whose application still expects it. The
    additive sections do not have that failure mode: an extra column nobody reads is harmless.
    """
    build_scratch()

    fresh_tables = {tuple(row) for row in rows(SCRATCH, TABLE_QUERY)}
    live_tables = {tuple(row) for row in rows(LIVE, TABLE_QUERY)}

    fresh_columns = {(r[0], r[1], r[2]): r for r in rows(SCRATCH, COLUMN_QUERY)}
    live_columns = {(r[0], r[1], r[2]): r for r in rows(LIVE, COLUMN_QUERY)}

    missing_tables = sorted(live_tables - fresh_tables)
    obsolete_tables = sorted(fresh_tables - live_tables)

    lines = []

    # A table in a schema the fresh instance has never heard of cannot be created. The schemas of the
    # initial migration are in schema.sql already; one introduced later (audit, #378) is not, and
    # CREATE TABLE audit."..." then fails on the very first line of a new install.
    missing_schemas = sorted({schema for schema, _ in missing_tables}
                             - {schema for schema, _ in fresh_tables})

    if missing_schemas:
        lines.append("-- Schemas a fresh instance never got, before the tables that live in them.")
        lines.extend(f"CREATE SCHEMA IF NOT EXISTS {schema};" for schema in missing_schemas)
        lines.append("")

    if missing_tables:
        all_creation, all_keys, all_wiring = [], [], []

        for schema, table in missing_tables:
            creation, keys, wiring = table_ddl(schema, table)
            all_creation.append(f"-- {schema}.{table}")
            all_creation.extend(creation)
            all_creation.append("")
            all_keys.extend(keys)
            all_wiring.extend(wiring)

        lines.append("-- Tables a fresh instance never got.")
        lines.extend(all_creation)
        lines.append("-- Primary and unique keys, before anything references them.")
        lines.extend(all_keys)
        lines.append("")
        lines.append("-- Foreign keys and indexes.")
        lines.extend(all_wiring)
        lines.append("")

    added, retyped, renullified, redefaulted = [], [], [], []

    for key, row in live_columns.items():
        schema, table, column = key

        if (schema, table) in missing_tables or key in DRIFT_COLUMNS:
            continue

        live_type = sql_type(row[3], row[4])

        if key not in fresh_columns:
            null = "" if row[5] == "YES" else " NOT NULL"
            default = f" DEFAULT {row[6]}" if row[6] else ""
            added.append(f'ALTER TABLE {schema}."{table}" '
                         f'ADD COLUMN IF NOT EXISTS "{column}" {live_type}{default}{null};')
            continue

        # Nullability is carried only on the ADD COLUMN above, so a column that later became nullable
        # kept its NOT NULL on every fresh instance forever. Orders stopped requiring a
        # customer, the development database was altered by hand, and a self-hosted instance went on
        # refusing every order that names none.
        if row[5] != fresh_columns[key][5]:
            renullified.append(nullability_change(schema, table, column, nullable=row[5] == "YES"))

        # `nextval(...)` is a serial column's own sequence and belongs to the table that created it,
        # never to a delta - the rest is a real default someone declared on the model.
        if row[6] != fresh_columns[key][6] and "nextval(" not in row[6] + fresh_columns[key][6]:
            redefaulted.append(default_change(schema, table, column, row[6]))

        fresh_type = sql_type(fresh_columns[key][3], fresh_columns[key][4])

        if fresh_type == live_type:
            continue

        # The guard has to compare the length as well, not just the type name. Comparing only
        # data_type meant a varchar(50) -> varchar(80) change never fired its own guard, so it was
        # re-emitted on every run and the delta never converged.
        length_test = (f"AND coalesce(character_maximum_length, -1) = {row[4] or -1}"
                       if row[4] or row[3] == "character varying" else "")
        guard = (f"IF NOT EXISTS (SELECT 1 FROM information_schema.columns "
                 f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
                 f"AND column_name = '{column}' AND data_type = '{row[3]}' {length_test}) THEN")
        alter = (f'  ALTER TABLE {schema}."{table}" ALTER COLUMN "{column}" '
                 f'TYPE {live_type} USING "{column}"::{live_type};')

        # An integer widened to text is an enum column being stored by name. A plain cast writes
        # the *ordinal* as text ("3" instead of "Completed"), so it is only safe while the table is
        # empty — which is the fresh-install case this file exists for. On a populated instance the
        # per-value CASE mapping is required, and that lives in its own script.
        if fresh_columns[key][3] in ("integer", "smallint", "bigint") \
                and live_type.startswith("character varying"):
            retyped.append(
                f"DO $retype$ BEGIN {guard}\n"
                f'  IF EXISTS (SELECT 1 FROM {schema}."{table}" LIMIT 1) THEN\n'
                f"    RAISE EXCEPTION 'Refusing to cast {schema}.{table}.{column} from a number to "
                f"text with rows present: the cast would store the ordinal instead of the member "
                f"name. Run Scripts/sql/one-shot/convert_enum_columns_to_strings.sql first.';\n"
                f"  END IF;\n"
                f"{alter}\n"
                f"END IF; END $retype$;")
        else:
            retyped.append(f"DO $retype$ BEGIN {guard}\n{alter}\nEND IF; END $retype$;")

    if added:
        lines.append("-- Columns added to existing tables since this file was last correct.")
        lines.extend(added)
        lines.append("")

    if retyped:
        lines.append("-- Columns whose type changed (the enum-as-string rule, widened text).")
        lines.extend(retyped)
        lines.append("")

    if renullified:
        lines.append("-- Columns whose nullability changed after this file shipped them.")
        lines.extend(renullified)
        lines.append("")

    if redefaulted:
        lines.append("-- Columns whose default changed after this file shipped them.")
        lines.extend(redefaulted)
        lines.append("")

    # The other direction of the column comparison: this file is append-only, so a column that was
    # dropped from the model stays in it forever and a fresh instance keeps creating it. verify_db_init
    # reports exactly that as "surplus".
    obsolete_columns = []
    obsolete_table_set = set(obsolete_tables)

    for key in fresh_columns:
        schema, table, column = key

        if (schema, table) in obsolete_table_set or key in live_columns:
            continue

        obsolete_columns.append(f'ALTER TABLE {schema}."{table}" DROP COLUMN IF EXISTS "{column}";')

    if not keep_obsolete:
        obsolete_columns, obsolete_tables = [], []

    if obsolete_columns:
        lines.append("-- Columns the model no longer has; a fresh instance should not create them.")
        lines.extend(sorted(obsolete_columns))
        lines.append("")

    if obsolete_tables:
        lines.append("-- Tables the model no longer has; a fresh instance should not create them.")

        for schema, table in obsolete_tables:
            lines.append(f'DROP TABLE IF EXISTS {schema}."{table}" CASCADE;')

        lines.append("")

    # Indexes and foreign keys that arrived after their table did. A brand-new table brings its own
    # along in the pg_dump above; one added to a table that already existed was invisible here, and
    # verify_db_init.py compares columns only - so a fresh instance quietly went without the
    # constraint. The self-reference on audit."ActivityEvents" (#378) was the first to hit this.
    wiring = missing_wiring(missing_tables)

    if wiring:
        lines.append("-- Indexes and constraints added to tables that already existed.")
        lines.extend(wiring)
        lines.append("")

    # The other direction, and the same reason the obsolete columns above exist: this file is
    # append-only, so an index an earlier block created outlives the change that replaced it.
    # IX_CustomerContacts_Customer_Type was renamed during a rework and a fresh instance
    # kept building the old one for months.
    obsolete_indexes = [] if not keep_obsolete else surplus_indexes()

    if obsolete_indexes:
        lines.append("-- Indexes this file created and the model no longer has.")
        lines.extend(obsolete_indexes)
        lines.append("")

    # After the columns they describe, and before the seed rows, which are the slow part.
    comments = missing_comments()

    if comments:
        lines.append("-- Table and column comments the model declares. The only documentation that")
        lines.append("-- travels with a dump, so a record reads without this application.")
        lines.extend(comments)
        lines.append("")

    # Last, because a seed row needs the table and every column it names to exist first.
    seeds = missing_seed_rows()

    if seeds:
        lines.append("-- Seed rows the model declares through HasData that a fresh instance misses.")
        lines.extend(seeds)

    # And after them the rows that are present but out of date: the block above only inserts what is
    # missing, and its ON CONFLICT DO NOTHING leaves an existing row exactly as it was (#418).
    changed, changed_count = changed_seed_values()

    if changed:
        lines.append("-- Seed values the model changed after these rows were shipped. Each statement")
        lines.append("-- is guarded on the previously shipped value, so an edited row keeps the")
        lines.append("-- instance owner's version.")
        lines.extend(changed)

    # Last of all, because it reads the rows everything above has just made possible.
    backfills, backfill_count = pending_backfills()

    if backfills:
        lines.append("-- Backfills: rows an existing instance has to have rewritten, not just a column")
        lines.append("-- added. Written by hand and carried verbatim - they cannot be derived from a")
        lines.append("-- schema comparison, and without them a self-hosted instance gets the new column")
        lines.append("-- empty forever while the development database has it filled.")
        lines.extend(backfills)

    return lines, {
        "tables": len(missing_tables), "columns": len(added),
        "retyped": len(retyped), "renullified": len(renullified),
        "redefaulted": len(redefaulted), "obsolete": len(obsolete_tables),
        "dropped": len(obsolete_columns), "wiring": len(wiring),
        "unwired": len(obsolete_indexes), "comments": len(comments),
        "seeds": sum(1 for line in seeds if line.startswith("INSERT")),
        "values": changed_count,
        "backfills": backfill_count,
    }


def pending_backfills():
    """The hand-written data migrations of Scripts/sql/backfill that schema.sql does not carry yet.

    Everything else in this file is derived by comparing two schemas, which is why it can never see a
    data migration: adding Products."Category" is a schema change and *filling* it from
    a lookup table is not. Emitted once, recognised by the file's own name in a marker comment,
    and each file has to be written so that running it a second time changes nothing - compose applies
    schema.sql on every `up`.
    """
    directory = os.path.join(SCRIPTS, "sql", "backfill")

    if not os.path.isdir(directory):
        return [], 0

    shipped = ""

    if os.path.exists(SCHEMA_FILE):
        with open(SCHEMA_FILE, encoding="utf-8") as handle:
            shipped = handle.read()

    lines = []
    count = 0

    for name in sorted(entry for entry in os.listdir(directory) if entry.endswith(".sql")):
        marker = f"-- backfill: {name}"

        if marker in shipped:
            continue

        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            lines.append(marker)
            lines.append(handle.read().strip())
            lines.append("")

        count += 1

    return lines, count


INDEX_QUERY = """
select schemaname, tablename, indexname, indexdef
from pg_indexes
where schemaname not in ('pg_catalog', 'information_schema')
order by 1, 2, 3
"""

# Foreign keys and checks.
#
# Not 'n': PostgreSQL 17 gives every NOT NULL a catalog entry with a generated name, and the same
# column can be "..._not_null" on one instance and "..._not_null1" on another - 1054 of them here,
# all noise. Nullability is compared column by column instead, which is where it belongs.
#
# Not 'u' either, and that one is a trap rather than noise: a unique constraint is backed by an index
# of the same name, which INDEX_QUERY above already emits. Adding the constraint as well would run
# ALTER TABLE ADD CONSTRAINT against a name that is already taken and fail the whole file. The two
# here arrive with their tables; a unique constraint that ever does go missing is reported by
# verify_db_init.py rather than silently half-created.
CONSTRAINT_QUERY = """
select n.nspname, c.relname, con.conname, pg_get_constraintdef(con.oid)
from pg_constraint con
join pg_class c on c.oid = con.conrelid
join pg_namespace n on n.oid = c.relnamespace
where con.contype in ('f', 'c') and n.nspname not in ('pg_catalog', 'information_schema')
order by 1, 2, 3
"""


def model_seed_rows():
    """The rows the configurations declare through HasData, grouped by table.

    Read out of the model by Scripts/csharp/model_seed_data.cs, never out of a database. The
    development database holds the same tables with rows someone edited, rows someone added, and -
    in diary."Diaries" - the developer's own diaries beside the templates; dumping it would ship
    personal data and local drift to everyone who installs this.
    """
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "dotnet.py"), "script", "model_seed_data"],
        cwd=REPO, text=True, capture_output=True)

    if result.returncode != 0:
        sys.stderr.write((result.stderr or result.stdout or "").strip() + "\n")
        sys.exit(result.returncode)

    grouped = {}

    for line in result.stdout.splitlines():
        match = re.match(r'INSERT INTO (\w+)\."([^"]+)"', line)

        if match:
            grouped.setdefault((match.group(1), match.group(2)), []).append(line)

    return grouped


def model_comments():
    """The table and column comments the configurations declare through HasComment.

    Out of the model for the same reason as the seed rows: EF writes a comment through a migration
    and this repository git-ignores its migrations, so one added to a configuration would reach the
    development database and nowhere else. #517 - four of them had already drifted that way.
    """
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "dotnet.py"), "script", "model_comments"],
        cwd=REPO, text=True, capture_output=True)

    if result.returncode != 0:
        sys.stderr.write((result.stderr or result.stdout or "").strip() + "\n")
        sys.exit(result.returncode)

    declared = {}

    for line in result.stdout.splitlines():
        parts = line.split("\t")

        if len(parts) == 4 and parts[3]:
            declared[(parts[0], parts[1], parts[2])] = parts[3]

    return declared


COMMENT_QUERY = """
select n.nspname, c.relname, coalesce(a.attname, ''), d.description
from pg_description d
join pg_class c on c.oid = d.objoid
join pg_namespace n on n.oid = c.relnamespace
left join pg_attribute a on a.attrelid = c.oid and a.attnum = d.objsubid
where n.nspname not in ('pg_catalog', 'information_schema')
"""


def missing_comments():
    """COMMENT ON statements for what the model declares and a fresh instance would not carry.

    No IF NOT EXISTS guard and none needed: COMMENT ON simply sets the text, so re-running it is a
    no-op. That makes this the least dangerous section of the whole block.
    """
    fresh = {(row[0], row[1], row[2]): row[3] for row in rows(SCRATCH, COMMENT_QUERY, check=False)}
    statements = []

    for (schema, table, column), text in sorted(model_comments().items()):
        if fresh.get((schema, table, column)) == text:
            continue

        target = f'COLUMN {schema}."{table}"."{column}"' if column else f'TABLE {schema}."{table}"'
        statements.append(f"COMMENT ON {target} IS '{text.replace(chr(39), chr(39) * 2)}';")

    return statements


INSERT_PATTERN = re.compile(
    r'INSERT INTO (\w+)\."([^"]+)" \((.*?)\) VALUES \((.*)\)(?: ON CONFLICT[^;]*)?;?\s*$')


def split_literals(text):
    """Split a VALUES list on its top-level commas.

    A plain split cannot do it: a description holds commas of its own, and a quoted string may hold
    a doubled quote. Depth is tracked for the parentheses of TIMESTAMPTZ and of any casts.
    """
    parts = []
    current = []
    quoted = False
    depth = 0
    index = 0

    while index < len(text):
        character = text[index]

        if quoted:
            current.append(character)

            if character == "'":
                # A doubled quote is one escaped quote, not the end of the string.
                if index + 1 < len(text) and text[index + 1] == "'":
                    current.append(text[index + 1])
                    index += 2
                    continue

                quoted = False
        elif character == "'":
            quoted = True
            current.append(character)
        elif character == "(":
            depth += 1
            current.append(character)
        elif character == ")":
            depth -= 1
            current.append(character)
        elif character == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(character)

        index += 1

    parts.append("".join(current).strip())

    return parts


def same_literal(left, right):
    """Whether two SQL literals say the same thing, however they are written.

    The blocks this generator emits and the older hand-written ones spell the same value differently:
    `TRUE` against `true`, and `TIMESTAMPTZ '2025-01-01T00:00:00Z'` against
    `'2025-01-01 00:00:00+00'`. Compared as text those are hundreds of differences that are not
    differences, and appending an UPDATE for each would bury the one real change among them.
    """
    left, right = left.strip(), right.strip()

    if left == right or left.upper() == right.upper():
        return True

    left_moment, right_moment = as_moment(left), as_moment(right)

    return left_moment is not None and left_moment == right_moment


def as_moment(literal):
    """A timestamp literal as a point in time, or None when it is not one."""
    text = re.sub(r"^(TIMESTAMPTZ|TIMESTAMP|DATE)\s+", "", literal.strip(), flags=re.IGNORECASE)

    if not text.startswith("'") or not text.endswith("'"):
        return None

    text = text[1:-1].strip().replace(" ", "T")

    # Postgres writes the offset as +00, which fromisoformat wants as +00:00.
    if re.search(r"[+-]\d{2}$", text):
        text += ":00"

    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_insert(line):
    """One INSERT statement as (schema, table, {column: literal}), or None.

    Both sides of the seed comparison are printed by Scripts/csharp/model_seed_data.cs in the same
    formatting - the model's rows now, and the rows already in schema.sql when they were emitted -
    so their literals can be compared as text and no value ever has to be interpreted.
    """
    match = INSERT_PATTERN.match(line.strip())

    if match is None:
        return None

    schema, table, column_list, value_list = match.groups()
    columns = [name.strip().strip('"') for name in column_list.split(",")]
    values = split_literals(value_list)

    if len(columns) != len(values):
        return None

    return schema, table, dict(zip(columns, values))


INSERT_BLOCK_PATTERN = re.compile(
    r'INSERT INTO (\w+)\."([^"]+)"\s*\(([^)]*)\)\s*VALUES\s*(.*)$', re.DOTALL | re.IGNORECASE)


def parse_insert_rows(statement):
    """Every row of one INSERT statement, as (schema, table, {column: literal}).

    The single-statement parser above reads what this generator emits: one row, one line. The blocks
    already in schema.sql are neither - they span lines and carry all nine seed rows in one VALUES
    list - so read line by line they parsed to nothing at all, and every row shipped that way counted
    as never shipped. The value comparison then skipped them, and the missing-row check compares by
    row count, which matched. That is how healthcare."PractitionerTypes"."Code" stayed empty on every
    fresh instance while the development database had it (#455).
    """
    match = INSERT_BLOCK_PATTERN.match(statement.strip())

    if match is None:
        return []

    schema, table, column_list, value_list = match.groups()
    columns = [name.strip().strip('"') for name in column_list.split(",")]

    values = re.sub(r"\s*ON CONFLICT.*$", "", value_list.strip(), flags=re.DOTALL | re.IGNORECASE)
    values = values.rstrip(";").strip()

    parsed = []

    for entry in split_literals(values):
        entry = entry.strip()

        if not entry.startswith("(") or not entry.endswith(")"):
            continue

        literals = split_literals(entry[1:-1])

        if len(literals) == len(columns):
            parsed.append((schema, table, dict(zip(columns, literals))))

    return parsed


UPDATE_PATTERN = re.compile(
    r'UPDATE (\w+)\."([^"]+)" SET "([^"]+)" = (.*) WHERE (.*);\s*$')


def parse_update(line):
    """One of this generator's own seed-value UPDATEs, as (schema, table, keys, column, value).

    Only the shape emitted below is recognised, which is the point: the file is read back to learn
    what it already delivers, and a statement this generator did not write is none of its business.
    """
    match = UPDATE_PATTERN.match(line.strip())

    if match is None:
        return None

    schema, table, column, value, where = match.groups()
    guard = f' AND "{column}" IS NOT DISTINCT FROM '

    if guard not in where:
        return None

    conditions = where[:where.rindex(guard)]
    keys = {}

    for condition in conditions.split(" AND "):
        name, _, literal = condition.partition(" = ")
        keys[name.strip().strip('"')] = literal.strip()

    return schema, table, keys, column, value


def shipped_seed_rows():
    """The seed rows deploy/db-init/schema.sql already delivers, keyed by table and primary key.

    This file is the whole of what a self-hosted instance receives, so what it says a row contains is
    the previously shipped state - and therefore the value an UPDATE may safely overwrite.

    Read front to back, applying both kinds of statement in order: the INSERT that first delivered a
    row, and every value UPDATE appended after it. Reading only the INSERTs would make each run
    rediscover the same difference and append the same statements again - the file would grow on
    every call and --check would never come back green.
    """
    shipped = {}

    for statement in seed_statements():
        for schema, table, columns in parse_insert_rows(statement):
            keys = primary_key(schema, table)

            if not keys or not all(key in columns for key in keys):
                continue

            identity = tuple(columns[key] for key in keys)

            # Every INSERT is ON CONFLICT DO NOTHING, so a later one for a row that is already there
            # delivers nothing: counting it made catalog."ProductTypes"."Code" look shipped
            # while a fresh instance kept the empty value of the first INSERT.
            shipped.setdefault((schema, table, identity), columns)

        updated = parse_update(statement)

        if updated is None:
            continue

        schema, table, key_values, column, value = updated
        keys = primary_key(schema, table)

        if not keys or not all(key in key_values for key in keys):
            continue

        row = shipped.get((schema, table, tuple(key_values[key] for key in keys)))

        if row is not None:
            row[column] = value

    return shipped


def seed_statements():
    """The INSERT and UPDATE statements of schema.sql, each as one string however many lines it spans.

    Everything else in the file is skipped by never starting a statement on it: a line that is not the
    beginning of an INSERT or an UPDATE cannot be the beginning of one that matters here.
    """
    statement = []

    with open(SCHEMA_FILE, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()

            if not statement and not stripped.upper().startswith(("INSERT INTO ", "UPDATE ")):
                continue

            statement.append(stripped)

            if not stripped.endswith(";"):
                continue

            yield " ".join(statement)
            statement = []


_COLUMN_DEFAULTS: dict[tuple[str, str], dict[str, str]] = {}


def constant_default(schema, table, column):
    """The column's default where it is a plain constant, otherwise None.

    Wanted for a column that is newer than the rows: every row that already existed took this value
    from the ALTER TABLE, so it is what an untouched row still holds and therefore what an UPDATE may
    overwrite. Anything with a call in it - now(), nextval(...) - is not a value a row can be
    compared against, and the column is left alone rather than guessed at.
    """
    if (schema, table) not in _COLUMN_DEFAULTS:
        found = rows(LIVE,
                     "select column_name, coalesce(column_default, '') "
                     "from information_schema.columns "
                     f"where table_schema = '{schema}' and table_name = '{table}'",
                     check=False)
        _COLUMN_DEFAULTS[(schema, table)] = {row[0]: row[1] for row in found}

    default = _COLUMN_DEFAULTS[(schema, table)].get(column, "")

    if not default or "(" in default:
        return None

    # information_schema spells a default out with its type: 'Mass'::character varying.
    return default.split("::", 1)[0].strip()


_PRIMARY_KEYS: dict[tuple[str, str], list[str]] = {}


def primary_key(schema, table):
    """The primary key columns of a table, in order."""
    if (schema, table) not in _PRIMARY_KEYS:
        found = rows(LIVE,
                     "select a.attname from pg_index i "
                     "join pg_class c on c.oid = i.indrelid "
                     "join pg_namespace n on n.oid = c.relnamespace "
                     "join pg_attribute a on a.attrelid = c.oid "
                     "and a.attnum = any(i.indkey) "
                     f"where n.nspname = '{schema}' and c.relname = '{table}' "
                     "and i.indisprimary order by array_position(i.indkey, a.attnum)",
                     check=False)
        _PRIMARY_KEYS[(schema, table)] = [row[0] for row in found]

    return _PRIMARY_KEYS[(schema, table)]


def changed_seed_values():
    """UPDATEs for seed rows whose values the model changed after they were shipped.

    The missing-row check above compares per table by row count, so a table whose count already
    matches is skipped whole - and a corrected name, a fixed description or a column backfilled with
    a real value reached the development database and no self-hosted instance at all (#418). The
    rows also travel as ON CONFLICT DO NOTHING, so re-emitting the INSERT would change nothing on an
    instance that already holds them.

    Every statement is guarded on the previously shipped value. A row whose value no longer matches
    is one the instance's owner edited, and theirs is the version that stands - the same rule that
    keeps an import from overwriting a name someone typed themselves.
    """
    shipped = shipped_seed_rows()
    statements = []
    updated = 0

    for (schema, table), inserts in sorted(model_seed_rows().items()):
        keys = primary_key(schema, table)

        if not keys:
            continue

        table_statements = []

        for insert in inserts:
            parsed = parse_insert(insert)

            if parsed is None:
                continue

            columns = parsed[2]

            if not all(key in columns for key in keys):
                continue

            identity = tuple(columns[key] for key in keys)
            was = shipped.get((schema, table, identity))

            if was is None:
                # Not shipped yet - the missing-rows block inserts it whole.
                continue

            where = " AND ".join(f'"{key}" = {columns[key]}' for key in keys)

            for column, value in columns.items():
                if column in keys:
                    continue

                if column in was:
                    previous = was[column]
                else:
                    # A column younger than the row: the shipped INSERT never named it, so every row
                    # that already existed took the ALTER TABLE's default and still holds it unless
                    # someone changed it. Added without a default, that value is NULL - which is a
                    # previous value like any other, and the one worth filling in. Skipping those left
                    # healthcare."PractitionerTypes"."Code" empty on every fresh instance while the
                    # development database had it (#455), and the columns check could not see it
                    # because the column itself was there.
                    previous = constant_default(schema, table, column)

                    if previous is None:
                        previous = "NULL"

                if same_literal(previous, value):
                    continue

                table_statements.append(
                    f'UPDATE {schema}."{table}" SET "{column}" = {value} '
                    f'WHERE {where} AND "{column}" IS NOT DISTINCT FROM {previous};')
                updated += 1

        if table_statements:
            statements.append(f"-- {schema}.{table}")
            statements.extend(table_statements)
            statements.append("")

    return statements, updated


def identity_column(schema, table):
    """The generated key column of a table, or None when it has none."""
    found = rows(LIVE, "select a.attname from pg_attribute a "
                       "join pg_class c on c.oid = a.attrelid "
                       "join pg_namespace n on n.oid = c.relnamespace "
                       f"where n.nspname = '{schema}' and c.relname = '{table}' "
                       "and a.attidentity <> ''")

    return found[0][0] if found else None


def seed_order(grouped):
    """The seeded tables, every table after the ones its rows point at.

    Alphabetical order is right until two seeded tables reference each other, and then it is silently
    wrong: the insert runs before the row it needs exists. The dependencies come from the foreign keys
    the live database actually has, so nothing here has to be kept in step by hand.

    A cycle - two tables referencing each other - is left in whatever order it came in rather than
    hanging: it cannot be resolved by ordering at all, and the file failing loudly on the insert is a
    better answer than this function looping.
    """
    references = {}

    for schema, table, _, definition in rows(LIVE, CONSTRAINT_QUERY):
        target = re.search(r'REFERENCES (\w+)\."([^"]+)"', definition)

        if target and (target.group(1), target.group(2)) != (schema, table):
            references.setdefault((schema, table), set()).add((target.group(1), target.group(2)))

    ordered = []
    pending = sorted(grouped)

    while pending:
        ready = [key for key in pending
                 if not (references.get(key, set()) & set(pending) - {key})]

        if not ready:
            ready = pending

        for key in ready:
            ordered.append((key, grouped[key]))
            pending.remove(key)

    return ordered


def missing_seed_rows():
    """INSERTs for the seed rows a fresh instance would not get.

    EF writes HasData rows through a migration and this repository git-ignores its migrations, so
    without this a seed added to a configuration reaches the development database and no self-hosted
    instance ever. #282 hit that: an institution needs a type, and the type table came up empty.

    Compared per table by row count. A fresh instance holds nothing but seed rows, so a table that
    is short of what the model declares is missing seeds - and one that matches needs nothing.

    Emitted parents first: sorting by table name put catalog."LookupListItems" ahead of the
    catalog."LookupLists" its rows reference, and a fresh instance died on the foreign key while
    the development database - where the rows were already there - noticed nothing.
    """
    statements = []

    for (schema, table), inserts in seed_order(model_seed_rows()):
        # check=False: a table created higher up in this very block does not exist in the scratch
        # database yet, and "no answer" is the same as "has none of them".
        fresh = rows(SCRATCH, f'select count(*) from {schema}."{table}"', check=False)

        if fresh and int(fresh[0][0]) >= len(inserts):
            continue

        statements.append(f"-- {schema}.{table}")
        # The rows carry explicit keys, and this file is replayed on every `compose up`, so it must
        # neither fail nor duplicate on an instance that already has them.
        statements.extend(f"{insert} ON CONFLICT DO NOTHING;" for insert in inserts)

        key = identity_column(schema, table)

        # Rows inserted with explicit keys leave the identity sequence where it was, so the next row
        # a user adds collides with the seed. GREATEST keeps the sequence off the negative ids the
        # diary templates use, which are below the sequence's minimum.
        if key:
            statements.append(
                f"SELECT setval(pg_get_serial_sequence('{schema}.\"{table}\"', '{key}'), "
                f'GREATEST((SELECT max("{key}") FROM {schema}."{table}"), 1));')

        statements.append("")

    return statements


def surplus_indexes():
    """Indexes a fresh instance builds that the development database no longer has.

    Primary keys are left alone: theirs belong to the constraint, and a table dropped from the model
    is handled by DROP TABLE above rather than by taking its key away first.
    """
    live = {row[2] for row in rows(LIVE, INDEX_QUERY)}

    return [f'DROP INDEX IF EXISTS {schema}."{name}";'
            for schema, _, name, _ in rows(SCRATCH, INDEX_QUERY)
            if name not in live and not name.startswith("PK_")]


def missing_wiring(missing_tables):
    """Indexes and foreign keys the live database has and a fresh instance would not get."""
    new_tables = set(missing_tables)
    statements = []

    fresh_indexes = {row[2]: row[3] for row in rows(SCRATCH, INDEX_QUERY)}

    for schema, table, name, definition in rows(LIVE, INDEX_QUERY):
        # A primary key's index comes with its constraint, not as a CREATE INDEX. A brand-new table
        # brings its indexes along in the pg_dump above.
        if name.startswith("PK_") or (schema, table) in new_tables:
            continue

        if name not in fresh_indexes:
            statements.append(definition.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
                              .replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1) + ";")
            continue

        # Same name, different index. Comparing names alone let an index that gained IsUnique or a
        # filter pass as present, so schema.sql went on creating the old shape and a self-hosted
        # instance never received the constraint the model relies on. The definitions come from
        # pg_indexes on both sides, which is Postgres's own normalised wording, so they compare
        # directly - a difference here is a real difference.
        if fresh_indexes[name] == definition:
            continue

        statements.append(f'DROP INDEX IF EXISTS {schema}."{name}";\n{definition};')

    fresh_constraints = {row[2] for row in rows(SCRATCH, CONSTRAINT_QUERY)}

    for schema, table, name, definition in rows(LIVE, CONSTRAINT_QUERY):
        if name in fresh_constraints or (schema, table) in new_tables:
            continue

        statements.append(
            constraint_guard(name, schema, table) + "\n"
            f'ALTER TABLE {schema}."{table}" ADD CONSTRAINT "{name}" {definition};\n'
            f"END IF; END $$;")

    return statements


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--append", action="store_true", help="Append the block to schema.sql")
    parser.add_argument("--check", action="store_true", help="Exit 1 if a delta exists")
    parser.add_argument("--no-obsolete", action="store_true",
                        help="Leave out the DROP COLUMN/DROP TABLE statements for objects the model "
                             "no longer has. Use it on a feature branch whose model still declares "
                             "something another branch has dropped: the comparison is against the "
                             "development database, which carries every branch, so those DROPs are "
                             "the other branch's work and applying them here would take a column "
                             "away from an instance that still needs it.")
    arguments = parser.parse_args()

    # A machine without the development database must not fail the check — the schema simply cannot
    # be compared there. Say so rather than passing quietly, and keep the failure exit for a real
    # delta, so a caller can tell "verified" from "could not verify".
    #
    # A machine without psql at all is the same situation, and it is not hypothetical: a hosted CI
    # runner may carry the client or may not, and a FileNotFoundError traceback would read as a bug
    # in this script rather than as an absent tool.
    if shutil.which("psql") is None:
        print("skipped: psql is not installed here, schema.sql not compared")
        return

    if psql(LIVE, ["-tAc", "select 1"], check=False).returncode != 0:
        print(f"skipped: {LIVE} at {HOST}:{PORT} is not reachable, schema.sql not compared")
        return

    undumped = undumped_routines()

    if undumped:
        for kind, schema, name in undumped:
            sys.stderr.write(f"not in the routine dump: {kind.lower()} {schema}.{name}\n")

        sys.stderr.write(f"Run: bash Scripts/dump/dump_db.sh {LIVE} {HOST} {USER}\n")

        if arguments.check:
            sys.exit(1)

    lines, counts = build_delta(keep_obsolete=not arguments.no_obsolete)
    summary = (f"{counts['tables']} table(s), {counts['columns']} column(s), "
               f"{counts['retyped']} type change(s), {counts['renullified']} nullability change(s), "
               f"{counts['redefaulted']} default change(s), {counts['dropped']} obsolete column(s), "
               f"{counts['obsolete']} obsolete table(s), {counts['wiring']} index/constraint(s), "
               f"{counts['unwired']} obsolete index(es), "
               f"{counts['comments']} comment(s), "
               f"{counts['seeds']} seed row(s), {counts['values']} seed value(s), "
               f"{counts['backfills']} backfill(s)")

    if not lines:
        print("deploy/db-init/schema.sql is in step with the database")
        return

    if arguments.check:
        sys.stderr.write(f"deploy/db-init/schema.sql is behind: {summary}\n"
                         f"Run: python3 Scripts/gen_db_init_delta.py --append\n")
        sys.exit(1)

    block = "\n".join([
        "",
        f"{MARKER} =========================================================================",
        "-- Generated by Scripts/gen_db_init_delta.py. Plain idempotent DDL rather than an EF",
        "-- migration block: a new migration id would make every guard above believe it had never",
        "-- run. Re-runnable, and a no-op on an instance that already has these objects.",
        f"-- Delta at generation time: {summary}.",
        "-- ============================================================================================",
        "",
        *lines,
    ])

    if arguments.append:
        with open(SCHEMA_FILE, "a", encoding="utf-8") as handle:
            handle.write(block)

        print(f"appended to deploy/db-init/schema.sql: {summary}")
    else:
        print(block)


if __name__ == "__main__":
    main()
