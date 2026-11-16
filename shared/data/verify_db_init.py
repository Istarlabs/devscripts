#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Prove that a fresh self-hosted instance actually gets the schema the application needs.

`deploy/db-init/schema.sql` plus the routines under `Scripts/dump/PostgreSQL` are the entire
install path: `deploy/compose.yaml` applies the first with psql and pipes the second through a sed
that turns `CREATE FUNCTION` into `CREATE OR REPLACE FUNCTION`. Nothing checked that the result
matched the model, and by #318 it did not — a fresh instance was missing 167 columns across 29
tables, and the routine replay aborted on the first file that lacked a terminating semicolon.

This does what compose does, against a throwaway database, and then compares:

    python3 Scripts/verify_db_init.py              build, replay, compare, then re-apply
    python3 Scripts/verify_db_init.py --keep       leave the throwaway database behind to inspect
    python3 Scripts/verify_db_init.py --reference model.sql
                                                   compare against model.sql + routines instead

--reference is for where the development database is out of reach, a CI runner above all. The file
is the schema the model describes (EF's GenerateCreateScript), built into a second throwaway database
together with the same routines; that database then stands in for the development one.

Six things have to hold, and each is a way the deployment has actually broken:

  1. schema.sql applies to an empty database without an error
  2. the routines replay as one concatenated stream — an unterminated file breaks the next one
  3. the resulting schema matches the development database, column for column
  4. every function, view and trigger of the development database exists on the fresh one
  5. so does every index, foreign key and check constraint
  6. applying schema.sql a second time changes nothing, because compose runs it on every `up`

Check 4 is the one that was missing while the dump fell behind by two search functions and the BMI
trigger: a routine that lives in the database and in no file replays without complaint, because
there is nothing to replay and nothing to fail on. Only comparing the two inventories notices it.

Check 5 is the other half of #355. The delta generator emits indexes and foreign keys, and nothing
proved it emitted them *all* - it did not emit check constraints at all, so a fresh instance went
without CK_Units_MatchAliases_IsArray and would have taken any JSON in a column the unit resolver
reads as an array.

Exit code is non-zero when any of them fails, so it can gate a pull request.

Two differences are expected and are not failures: the columns listed in EXPECTED_ABSENT, which are
local drift under #316 and deliberately not shipped.
"""
import argparse
import glob
import os
import re
import subprocess
import sys
from pathlib import Path

from project import project

SCRIPTS = str(project.root / "Scripts")
REPO = str(project.root)
SCHEMA_FILE = str(project.path("db_init.schema_file", "deploy/db-init/schema.sql"))
ROUTINES = str(project.path("db_init.routines", "Scripts/dump/PostgreSQL"))

HOST = project.env("DB_HOST", project.get("database.host", "localhost"))
PORT = str(project.env("DB_PORT", project.get("database.port", "5432")))
LIVE = project.env("DB_NAME", project.get("database.name", project.name))
USER = project.env("DB_USER", project.get("database.user", os.environ.get("USER", "postgres")))

ENV = project.psql_environment()

# Present on the development database, deliberately not shipped to a fresh instance.
#
# Empty since #316 — loinccode and closure were the last two and are mapped now, so a fresh instance
# is expected to match the development database exactly. Kept because the situation recurs.
EXPECTED_ABSENT: set[str] = set()

# Nullability and the default belong in the comparison, not only the type: schema.sql carried
# sales."Orders"."CustomerId" as NOT NULL long after the model made it optional, and this
# check called that "in step" because both sides said `integer`. A fresh instance refused every
# order without a customer for as long as that went unnoticed, and defaulted "Status" to `0`.
#
# A serial column's own sequence is excluded: `nextval('...')` names the sequence, and comparing it
# would report every such column as different for no reason.
COLUMN_QUERY = ("select table_schema || '.' || table_name || '.' || column_name || ' ' || data_type "
                "|| coalesce('(' || character_maximum_length || ')', '') "
                "|| case when is_nullable = 'YES' then ' null' else ' not null' end "
                "|| case when column_default is null or column_default like 'nextval(%' then '' "
                "else ' default ' || column_default end "
                "from information_schema.columns where table_schema not in "
                "('pg_catalog','information_schema') and table_name <> '__EFMigrationsHistory' "
                "order by 1")

# Identified by their signature rather than their name: an overload the dump lost is a missing
# routine like any other, and two functions of one name would otherwise cancel each other out.
ROUTINE_QUERY = (
    "select 'function ' || n.nspname || '.' || p.proname "
    "|| '(' || pg_get_function_identity_arguments(p.oid) || ')' "
    "from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
    "where n.nspname not in ('pg_catalog','information_schema') "
    "union all "
    "select 'view ' || schemaname || '.' || viewname from pg_views "
    "where schemaname not in ('pg_catalog','information_schema') "
    "union all "
    "select 'trigger ' || n.nspname || '.' || c.relname || '.' || t.tgname "
    "from pg_trigger t join pg_class c on c.oid = t.tgrelid "
    "join pg_namespace n on n.oid = c.relnamespace where not t.tgisinternal "
    "order by 1")

# Compared by definition, not by name: an index that gained IsUnique or a filter keeps its name, and
# comparing names alone let exactly that pass as present.
#
# NOT NULL constraints are left out. PostgreSQL 17 gives each of them a catalog entry with a
# generated name, and the same column is "..._not_null" on one instance and "..._not_null1" on
# another - 1054 of them here, every one a false difference. What they express is compared column by
# column in the step before this one.
WIRING_QUERY = (
    "select 'index ' || schemaname || '.' || indexname || ' :: ' || indexdef from pg_indexes "
    "where schemaname not in ('pg_catalog','information_schema') and tablename <> '__EFMigrationsHistory' "
    "union all "
    "select 'constraint ' || n.nspname || '.' || con.conname || ' :: ' || pg_get_constraintdef(con.oid) "
    "from pg_constraint con join pg_namespace n on n.oid = con.connamespace "
    "join pg_class c on c.oid = con.conrelid "
    "where con.contype <> 'n' and n.nspname not in ('pg_catalog','information_schema') "
    "and c.relname <> '__EFMigrationsHistory' "
    "order by 1")


# On a fresh instance every row is a seeded one, so a row whose "Code" is empty was seeded without it:
# the column matched in the comparison above while the value was never delivered.
EMPTY_CODE_QUERY = """
with code_tables as materialized (
    select c.table_schema, c.table_name,
           (xpath('/row/n/text()', query_to_xml(
               format('select count(*) as n from %I.%I where "Code" = ''''', c.table_schema, c.table_name),
               false, true, '')))[1]::text::int as empty_rows
    from information_schema.columns c
    join information_schema.tables t
      on t.table_schema = c.table_schema and t.table_name = c.table_name and t.table_type = 'BASE TABLE'
    where c.column_name = 'Code' and c.data_type like 'character%'
)
select table_schema || '.' || table_name || ' (' || empty_rows || ')'
from code_tables where empty_rows > 0 order by 1
"""

# Seeded rows carry explicit ids and do not move their sequence, so on a fresh instance a sequence behind its
# table means the next insert tries an id that is taken (found on two lookup tables).
SEQUENCES_BEHIND_QUERY = """
with serial as materialized (
    select n.nspname as s, c.relname as t, a.attname as col,
           pg_get_serial_sequence(format('%I.%I', n.nspname, c.relname), a.attname) as seq
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
    where c.relkind = 'r' and n.nspname not in ('pg_catalog', 'information_schema')
      and pg_get_serial_sequence(format('%I.%I', n.nspname, c.relname), a.attname) is not null
), measured as materialized (
    select s, t, col,
           (xpath('/row/m/text()', query_to_xml(format('select max(%I)::text as m from %I.%I', col, s, t), false, true, '')))[1]::text as max_id,
           (xpath('/row/v/text()', query_to_xml(format('select last_value::text as v from %s', seq), false, true, '')))[1]::text as last_value
    from serial
)
select s || '.' || t || '.' || col || ' (sequence at ' || last_value || ', largest id ' || max_id || ')'
from measured where max_id is not null and last_value::bigint < max_id::bigint order by 1
"""


def psql(database, arguments, check=True):
    command = ["psql", "-h", HOST, "-p", PORT, "-U", USER, "-d", database,
               "-v", "ON_ERROR_STOP=1", *arguments]
    result = subprocess.run(command, cwd=REPO, env=ENV, text=True, capture_output=True)

    if check and result.returncode != 0:
        sys.stderr.write((result.stderr or result.stdout or "").strip() + "\n")
        sys.exit(result.returncode)

    return result


def errors_in(result):
    return [line for line in (result.stderr + result.stdout).splitlines()
            if re.match(r"^(psql:)?.*\bERROR\b", line)]


def step(number, text):
    print(f"[{number}/8] {text}")


def routine_files():
    """The replay order compose uses: a trigger may only name a function that already exists."""
    return sorted(glob.glob(f"{ROUTINES}/*/Functions/*.sql")) \
        + sorted(glob.glob(f"{ROUTINES}/*/Views/*.sql")) \
        + sorted(glob.glob(f"{ROUTINES}/*/Triggers/*.sql"))


def check_terminators():
    """A file whose last statement lacks a semicolon swallows the next file in the stream."""
    unterminated = []

    for path in sorted(routine_files()):
        meaningful = "\n".join(line for line in Path(path).read_text(encoding="utf-8").splitlines()
                               if line.strip() and not line.strip().startswith("--"))

        if meaningful and not meaningful.rstrip().endswith(";"):
            unterminated.append(os.path.relpath(path, REPO))

    return unterminated


def replay_routines(database):
    """Concatenate the routines the way deploy/compose.yaml does and pipe them into psql."""
    paths = routine_files()
    stream = "\n".join(
        re.sub(r"^CREATE (VIEW|FUNCTION|PROCEDURE|TRIGGER)", r"CREATE OR REPLACE \1",
               Path(path).read_text(encoding="utf-8"), flags=re.MULTILINE)
        for path in paths)

    command = ["psql", "-h", HOST, "-p", PORT, "-U", USER, "-d", database, "-v", "ON_ERROR_STOP=1"]

    return subprocess.run(command, cwd=REPO, env=ENV, text=True, input=stream, capture_output=True), len(paths)


def columns(database):
    result = psql(database, ["-tA", "-c", COLUMN_QUERY])

    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def routines(database):
    result = psql(database, ["-tA", "-c", ROUTINE_QUERY])

    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def wiring(database):
    result = psql(database, ["-tA", "-c", WIRING_QUERY])

    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def build_reference(scratch, sql_file):
    """The stand-in for the development database: the model's schema plus the same routines.

    A failure here is not a finding about schema.sql, so it stops the run instead of being counted.
    """
    reference = f"{scratch}_reference"

    print(f"building {reference} from {sql_file} and the routines")
    psql("postgres", ["-c", f"DROP DATABASE IF EXISTS {reference}"])
    psql("postgres", ["-c", f"CREATE DATABASE {reference}"])

    for result in (psql(reference, ["-f", sql_file], check=False), replay_routines(reference)[0]):
        if result.returncode != 0 or errors_in(result):
            for line in errors_in(result)[:5] or [(result.stderr or result.stdout).strip()]:
                print(f"      {line}")

            sys.exit(f"refused: {reference} could not be built, so there is nothing to compare against")

    return reference


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database",
                        default=project.get("db_init.check_database",
                                            f"{project.env_prefix.lower()}_db_init_check"),
                        help="Name of the throwaway database")
    parser.add_argument("--keep", action="store_true", help="Do not drop it afterwards")
    parser.add_argument("--reference", metavar="SQL_FILE",
                        help="Compare against a database built from this file and the routines, "
                             "instead of the development database")
    arguments = parser.parse_args()

    scratch = arguments.database
    failures = []
    reference = build_reference(scratch, arguments.reference) if arguments.reference else LIVE

    step(1, f"building {scratch} from deploy/db-init/schema.sql")
    psql("postgres", ["-c", f"DROP DATABASE IF EXISTS {scratch}"])
    psql("postgres", ["-c", f"CREATE DATABASE {scratch}"])

    # No -1: the bundle opens its own transactions, and an inner COMMIT would close an outer one.
    applied = psql(scratch, ["-f", SCHEMA_FILE], check=False)

    if applied.returncode != 0 or errors_in(applied):
        failures.append("schema.sql did not apply cleanly")
        for line in errors_in(applied)[:5]:
            print(f"      {line}")
    else:
        print("      applied without error")

    step(2, "replaying the routines as one stream, as compose does")
    unterminated = check_terminators()

    if unterminated:
        failures.append(f"{len(unterminated)} routine file(s) without a terminating semicolon")
        for name in unterminated[:5]:
            print(f"      {name}")

    replayed, count = replay_routines(scratch)

    if replayed.returncode != 0 or errors_in(replayed):
        failures.append("the routine replay failed")
        for line in errors_in(replayed)[:5]:
            print(f"      {line}")
    else:
        print(f"      {count} routine file(s) replayed without error")

    step(3, f"comparing against {reference}")
    fresh, live = columns(scratch), columns(reference)
    # Split on the first space, not the last: the data type itself contains one ("character varying").
    missing = {entry for entry in live - fresh
               if entry.split(" ", 1)[0] not in EXPECTED_ABSENT}
    surplus = fresh - live

    if missing or surplus:
        failures.append(f"{len(missing)} column(s) missing, {len(surplus)} surplus")
        for entry in sorted(missing)[:10]:
            print(f"      missing  {entry}")
        for entry in sorted(surplus)[:10]:
            print(f"      surplus  {entry}")
    else:
        print(f"      {len(fresh)} columns match "
              f"({len(EXPECTED_ABSENT)} expected absences skipped)")

    step(4, "comparing the functions, views and triggers")
    fresh_routines, live_routines = routines(scratch), routines(reference)
    unshipped = live_routines - fresh_routines
    extra = fresh_routines - live_routines

    if unshipped or extra:
        failures.append(f"{len(unshipped)} routine(s) missing, {len(extra)} surplus")
        for entry in sorted(unshipped)[:10]:
            print(f"      missing  {entry}")
        for entry in sorted(extra)[:10]:
            print(f"      surplus  {entry}")
        print(f"      refresh the dump: bash Scripts/dump/dump_db.sh {LIVE} {HOST} {USER}")
    else:
        print(f"      {len(fresh_routines)} routines match")

    step(5, "comparing the indexes, foreign keys and check constraints")
    fresh_wiring, live_wiring = wiring(scratch), wiring(reference)
    unwired = live_wiring - fresh_wiring
    overwired = fresh_wiring - live_wiring

    if unwired or overwired:
        failures.append(f"{len(unwired)} index/constraint(s) missing, {len(overwired)} surplus")
        for entry in sorted(unwired)[:10]:
            print(f"      missing  {entry[:140]}")
        for entry in sorted(overwired)[:10]:
            print(f"      surplus  {entry[:140]}")
        print("      run: python3 Scripts/gen_db_init_delta.py --append")
    else:
        print(f"      {len(fresh_wiring)} indexes and constraints match")

    step(6, "looking for seeded rows left with an empty code")
    empty = [line.strip() for line in psql(scratch, ["-tA", "-c", EMPTY_CODE_QUERY]).stdout.splitlines()
             if line.strip()]

    if empty:
        failures.append(f"{len(empty)} table(s) hold seeded rows with an empty Code")
        for entry in empty[:10]:
            print(f"      {entry}")
        print("      run: python3 Scripts/gen_db_init_delta.py --append")
    else:
        print("      no seeded row without a code")

    step(7, "looking for sequences behind the largest key of their table")
    behind = [line.strip() for line in psql(scratch, ["-tA", "-c", SEQUENCES_BEHIND_QUERY]).stdout.splitlines()
              if line.strip()]

    if behind:
        failures.append(f"{len(behind)} sequence(s) are behind their table")
        for entry in behind[:10]:
            print(f"      {entry}")
        print("      a setval after the seed rows belongs in schema.sql (gen_db_init_delta.py emits it)")
    else:
        print("      every sequence is at or past the maximum of its table")

    step(8, "applying schema.sql a second time, as compose does on every up")
    again = psql(scratch, ["-f", SCHEMA_FILE], check=False)

    if again.returncode != 0 or errors_in(again):
        failures.append("schema.sql is not re-runnable")
        for line in errors_in(again)[:5]:
            print(f"      {line}")
    else:
        after = columns(scratch)
        if after != fresh:
            failures.append("the second run changed the schema")
        else:
            print("      no error, no change")

    if not arguments.keep:
        psql("postgres", ["-c", f"DROP DATABASE IF EXISTS {scratch}"])

        if arguments.reference:
            psql("postgres", ["-c", f"DROP DATABASE IF EXISTS {reference}"])

    print()

    if failures:
        for failure in failures:
            print(f"FAILED: {failure}")
        sys.exit(1)

    print("deploy/db-init/schema.sql produces the schema the application expects")


if __name__ == "__main__":
    main()
