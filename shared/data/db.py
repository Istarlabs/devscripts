#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""One entry point for the routine PostgreSQL chores in a repository.

Every query against the development database used to be a hand-built `psql -h … -U … -d … -tAc '…'`:
the connection retyped each time, the SQL wrapped in nested shell quoting that breaks the moment a
literal contains an apostrophe, and each unique line asking for permission again.

The connection comes from the [database] block of Scripts/project.toml, overridable per environment
variable ({PREFIX}_DB_HOST / _DB_PORT / _DB_NAME / _DB_USER, where the prefix is the project's
env_prefix). The password is never passed here — it stays in ~/.pgpass, which libpq reads on its own.
Note libpq silently ignores a .pgpass that is group- or world-readable: it has to be chmod 600.

    python3 Scripts/db.py connection                  who and where, without touching the password
    python3 Scripts/db.py query -c "select 1"         run SQL, tab-separated
    python3 Scripts/db.py query --file report.sql     run SQL from a file
    python3 Scripts/db.py query -c "..." --format csv|table|tsv

    python3 Scripts/db.py count sales.Orders sales.Customers
    python3 Scripts/db.py columns sales.Orders
    python3 Scripts/db.py enum-audit                  every enum column still stored as an integer
    python3 Scripts/db.py active-audit                every IsActive column, its default, its false rows
    python3 Scripts/db.py apply Scripts/some.sql      run a script as one transaction
    python3 Scripts/db.py apply x.sql --target production   any subcommand, on a [database.targets.NAME]

    python3 Scripts/db.py schema-diff other_db        tables and columns that differ from other_db
    python3 Scripts/db.py compare other_db --show 5   rows per primary key: only here, only there,
                                                      differing, and in which columns
    python3 Scripts/db.py compare other_db --map a.New=a.Old --ignore ModifiedOn --table a.New

Both compare against another database on the same server and connection. `compare` reads timestamps
as instants (local time and UTC for the same moment are equal) and names, instead of counting, a column
whose type differs between the two - an enum stored as a number in one and as its name in the other.

`enum-audit` reads the columns it checks from database.enum_columns in project.toml - the mapping
lives in C# fluent API this script cannot read, so it has to be stated somewhere.

A second server is a named target: `--target production` reads [database.targets.production] of
project.toml - `ssh_host`, `container` (the postgres container there) and optionally `name` and `user` -
and runs psql inside that container over ssh, as the deploy target does for the image. The server's
postgres is not published, so nothing else is needed; `apply` sends the file through stdin.

Identifiers are quoted for you: the schema uses PascalCase, so `sales.Orders` is sent as
sales."Orders".

`apply` wraps the script in a single transaction with ON_ERROR_STOP, so a script either lands whole
or not at all. Exit code is non-zero when psql fails, so it can gate a chain.
"""
import argparse
import csv
import io
import json
import os
import re
import shlex
import subprocess
import sys

from project import project

REPO = str(project.root)

HOST = project.env("DB_HOST", project.get("database.host", "localhost"))
PORT = str(project.env("DB_PORT", project.get("database.port", "5432")))
NAME = project.env("DB_NAME", project.get("database.name", project.name))
USER = project.env("DB_USER", project.get("database.user", os.environ.get("USER", "postgres")))

ENV = project.psql_environment()

TARGET = None

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# compare reads whole rows as one CSV field, and a row holding an image or a document outgrows the csv
# module's default limit of 128 KiB.
csv.field_size_limit(sys.maxsize)

# The enum-backed columns, taken from the entity configurations. Stated in project.toml rather than
# derived, because the mapping lives in C# fluent API that this script cannot read.
ENUM_COLUMNS = [tuple(entry) for entry in project.get("database.enum_columns", [])]


def select_target(name):
    """Point every later psql call at [database.targets.NAME]: a psql run inside a container over ssh."""
    global TARGET, NAME, USER

    section = project.get(f"database.targets.{name}")

    if section is None:
        sys.stderr.write(f"refused: {project.config_file} has no [database.targets.{name}].\n")
        sys.exit(1)

    TARGET = {"ssh_host": section["ssh_host"], "container": section["container"]}
    NAME = section.get("name", NAME)
    USER = section.get("user", USER)


def psql(arguments, capture=True, check=True, database=None):
    arguments = list(arguments)
    stdin = None

    if TARGET:
        # The server's postgres is not reachable from here, and the file of `apply` lives on this side:
        # its content goes in through stdin instead of -f.
        if "-f" in arguments:
            position = arguments.index("-f")
            with open(arguments[position + 1], encoding="utf-8") as script:
                stdin = script.read()
            del arguments[position:position + 2]

        remote = ["podman", "exec", "-i", TARGET["container"], "psql", "-U", USER, "-d", database or NAME,
                  "-v", "ON_ERROR_STOP=1", *arguments]
        command = ["ssh", TARGET["ssh_host"], shlex.join(remote)]
    else:
        command = ["psql", "-h", HOST, "-p", PORT, "-U", USER, "-d", database or NAME, "-v", "ON_ERROR_STOP=1",
                   *arguments]

    result = subprocess.run(command, cwd=REPO, env=ENV, text=True, capture_output=capture, input=stdin)

    if check and result.returncode != 0:
        sys.stderr.write((result.stderr or result.stdout or "").strip() + "\n")
        sys.exit(result.returncode)

    return result


def rows(sql, database=None):
    """
    Run a query and return its rows as lists of strings.

    Reads CSV rather than splitting on a separator: a column default or a description can contain
    the separator, a quote or a newline, and a naive split then loses or misaligns fields.
    """
    result = psql(["--csv", "-t", "-c", sql], database=database)

    return [row for row in csv.reader(io.StringIO(result.stdout)) if row]


def quote(table):
    """schema.Table -> "schema"."Table", rejecting anything that is not a plain identifier pair.

    The schema is quoted too: a PascalCase schema would otherwise fold to lower case, and "User" is a
    reserved word. A lower-case schema reads the same either way."""
    parts = table.split(".")

    if len(parts) != 2 or not all(IDENTIFIER.match(part) for part in parts):
        sys.stderr.write(f"refused: '{table}' is not a schema.Table identifier\n")
        sys.exit(1)

    return f'"{parts[0]}"."{parts[1]}"'


def statement(arguments):
    if arguments.file:
        path = arguments.file if os.path.isabs(arguments.file) else os.path.join(REPO, arguments.file)

        return open(path, encoding="utf-8").read()

    if arguments.sql:
        return arguments.sql

    sys.stderr.write("refused: need SQL: -c \"...\" or --file path\n")
    sys.exit(1)


def command_connection(_arguments):
    if TARGET:
        print(f"host {TARGET['ssh_host']}, container {TARGET['container']}")
    else:
        print(f"host {HOST}:{PORT}")

    print(f"db   {NAME}")
    print(f"user {USER}")

    pgpass = os.path.expanduser("~/.pgpass")

    if os.path.exists(pgpass):
        mode = oct(os.stat(pgpass).st_mode & 0o777)[2:]
        note = "" if mode == "600" else "  <- libpq ignores this, needs chmod 600"
        print(f"pgpass present, mode {mode}{note}")
    else:
        print("pgpass missing")

    version = rows("select version()")
    print()
    print(version[0][0] if version else "(no answer)")


def command_query(arguments):
    sql = statement(arguments)

    if arguments.format == "table":
        print(psql(["-c", sql]).stdout.rstrip())
    elif arguments.format == "csv":
        print(psql(["--csv", "-c", sql]).stdout.rstrip())
    else:
        print(psql(["-tAF", "\t", "-c", sql]).stdout.rstrip())


def command_count(arguments):
    parts = [f"select '{table}' as table_name, count(*) from {quote(table)}"
             for table in arguments.table]
    result = rows(" union all ".join(parts))

    width = max((len(row[0]) for row in result), default=0)

    for name, total in result:
        print(f"{name.ljust(width)}  {total:>10}")


def command_columns(arguments):
    # Only for the identifier check — the query below reads information_schema by name.
    quote(arguments.table)
    schema, table = arguments.table.split(".", 1)

    result = rows(
        "select column_name, data_type, is_nullable, coalesce(column_default, '') "
        "from information_schema.columns "
        f"where table_schema = '{schema}' and table_name = '{table}' "
        "order by ordinal_position")

    if not result:
        print(f"no such table: {arguments.table}")
        return

    width = max(len(row[0]) for row in result)

    for name, data_type, nullable, default in result:
        null = "null" if nullable == "YES" else "not null"
        print(f"{name.ljust(width)}  {data_type:<28} {null:<9} {default}")


def command_enum_audit(_arguments):
    if not ENUM_COLUMNS:
        print(f"{project.config_file} lists no database.enum_columns - nothing to audit")
        return

    conditions = " or ".join(
        f"(table_schema = '{schema}' and table_name = '{table}' and column_name = '{column}')"
        for schema, table, column in ENUM_COLUMNS)

    result = rows("select table_schema, table_name, column_name, data_type "
                  "from information_schema.columns "
                  f"where {conditions} order by table_schema, table_name")

    integers = [row for row in result if row[3] in ("integer", "smallint", "bigint")]

    for schema, table, column, data_type in result:
        mark = "INT " if row_is_integer(data_type) else "ok  "
        print(f"{mark}{schema}.{table}.{column}  {data_type}")

    print()
    print(f"{len(integers)} of {len(result)} enum column(s) still stored as an integer")

    if len(result) != len(ENUM_COLUMNS):
        missing = len(ENUM_COLUMNS) - len(result)
        print(f"note: {missing} configured column(s) not found in the database")

    sys.exit(1 if integers else 0)


def row_is_integer(data_type):
    return data_type in ("integer", "smallint", "bigint")


def command_active_audit(_arguments):
    """
    Every IsActive column, its default, and how many rows are switched off or unset.

    Written because answering "why is this record inactive" took a hand-built information_schema
    query with query_to_xml in it (#731), after the only administrator account turned out to be
    deactivated and the sign-in said "wrong password" about it. A column without a database default
    is the part worth seeing: 35 of the 41 default to true, and a row inserted by anything that is
    not EF - a seed, a backfill - lands inactive in the rest.

    A null is counted apart from a false on purpose. In the two hospital tables it means the INEK
    record predates the flag, which is neither active nor inactive, and a filter written as
    "IsActive = true" drops those rows without saying so.
    """
    # query_to_xml runs the count per table from inside one statement, which beats emitting 41
    # queries or building a UNION ALL as text.
    counted = ("(xpath('/row/c/text()', query_to_xml("
               "format('select count(*) as c from %I.%I{condition}', c.table_schema, c.table_name), "
               "false, true, '')))[1]::text::int")

    result = rows(
        "select c.table_schema, c.table_name, "
        + counted.format(condition="") + ", "
        + counted.format(condition=' where \"IsActive\" = false') + ", "
        + counted.format(condition=' where \"IsActive\" is null') + ", "
        "coalesce(c.column_default, '-'), c.is_nullable "
        "from information_schema.columns as c "
        "where c.column_name = 'IsActive' "
        "order by c.table_schema, c.table_name")

    if not result:
        print("no IsActive column anywhere - nothing to audit")
        return

    width = max(len(f"{row[0]}.{row[1]}") for row in result)
    without_default = []
    total_false = 0
    total_null = 0

    for schema, table, total, false_rows, null_rows, default, nullable in result:
        name = f"{schema}.{table}"
        mark = "    " if default == "true" else "DEF "

        if default != "true":
            without_default.append(name)

        total_false += int(false_rows)
        total_null += int(null_rows)

        nulls = f", {null_rows} null" if int(null_rows) else ""
        print(f"{mark}{name.ljust(width)}  {total:>6} row(s), {false_rows} false{nulls}"
              f"   default {default}{'' if nullable == 'NO' else ', nullable'}")

    print()
    print(f"{total_false} row(s) false, {total_null} null, across {len(result)} column(s)")

    if without_default:
        print(f"{len(without_default)} without a database default: {', '.join(without_default)}")

    # Reporting, not gating: a false row is usually somebody's decision. A missing default is the
    # thing to fix, and it is named above rather than turned into an exit code that would fail every
    # run until every last table is dealt with.


def application_schemas(alias):
    """The condition that leaves out PostgreSQL's own schemas, for the given table alias."""
    return (f"{alias}.table_schema not in ('pg_catalog', 'information_schema') "
            f"and {alias}.table_schema not like 'pg_toast%'")


def column_types(database):
    """{schema.Table: {column: (data_type, max_length, nullable)}} of every base table in a database."""
    result = rows("select c.table_schema, c.table_name, c.column_name, c.data_type, "
                  "coalesce(c.character_maximum_length::text, ''), c.is_nullable "
                  "from information_schema.columns c "
                  "join information_schema.tables t using (table_schema, table_name) "
                  f"where t.table_type = 'BASE TABLE' and {application_schemas('c')} "
                  "order by 1, 2, c.ordinal_position", database)
    tables = {}

    for schema, table, column, data_type, length, nullable in result:
        tables.setdefault(f"{schema}.{table}", {})[column] = (data_type, length, nullable)

    return tables


def primary_keys(database):
    """{schema.Table: [key columns in order]} for every table that has a primary key."""
    result = rows("select kcu.table_schema, kcu.table_name, kcu.column_name "
                  "from information_schema.table_constraints tc "
                  "join information_schema.key_column_usage kcu "
                  "on kcu.constraint_name = tc.constraint_name and kcu.table_schema = tc.table_schema "
                  f"where tc.constraint_type = 'PRIMARY KEY' and {application_schemas('kcu')} "
                  "order by 1, 2, kcu.ordinal_position", database)
    keys = {}

    for schema, table, column in result:
        keys.setdefault(f"{schema}.{table}", []).append(column)

    return keys


def there_to_here(arguments):
    """{table name in the other database: its name here}, from the --map HERE=THERE options."""
    return {there: here for here, there in (entry.split("=", 1) for entry in arguments.map or [])}


def table_pairs(arguments, here, there):
    """(table here, table there) for every table both have, plus each --map old=new rename."""
    here_name = there_to_here(arguments)
    pairs = [(here_name.get(name, name), name) for name in there if here_name.get(name, name) in here]

    if arguments.table:
        wanted = set(arguments.table)
        pairs = [pair for pair in pairs if pair[0] in wanted or pair[1] in wanted]

    return sorted(set(pairs))


def command_schema_diff(arguments):
    """
    Tables and columns that differ between this database and another one on the same server.

    Written for checking a hand-written migration: build one database from the model, migrate a copy
    of the real one, and this has to come back empty.
    """
    here, there = column_types(NAME), column_types(arguments.other)
    here_name = there_to_here(arguments)
    there = {here_name.get(name, name): columns for name, columns in there.items()}
    differences = 0

    for name in sorted(set(here) - set(there)):
        print(f"only in {NAME:<14} {name}")
        differences += 1

    for name in sorted(set(there) - set(here)):
        print(f"only in {arguments.other:<14} {name}")
        differences += 1

    for name in sorted(set(here) & set(there)):
        for column in sorted(set(here[name]) | set(there[name])):
            mine, theirs = here[name].get(column), there[name].get(column)

            if mine != theirs:
                describe = lambda c: "-" if c is None else f"{c[0]}{f'({c[1]})' if c[1] else ''}{'' if c[2] == 'YES' else ' not null'}"
                print(f"differs  {name}.{column}: {NAME} {describe(mine)} | {arguments.other} {describe(theirs)}")
                differences += 1

    print(f"{differences} difference(s)")
    sys.exit(1 if differences else 0)


COMPARABLE = {
    "timestamp without time zone": "extract(epoch from {0}::timestamptz)",
    "timestamp with time zone": "extract(epoch from {0})",
    "numeric": "trim_scale({0})",
}


def comparable_type(data_type):
    """Types that hold the same kind of value: both timestamp types name an instant."""
    return "timestamp" if data_type.startswith("timestamp") else data_type


def normalized(column, data_type, ignore_case, timezone=None):
    """The expression a column is compared by: a timestamp as the instant it names, numeric without scale."""
    quoted = f'"{column}"'

    if data_type == "timestamp without time zone" and timezone:
        expression = f"extract(epoch from {quoted} at time zone '{timezone}')"
    else:
        expression = COMPARABLE.get(data_type, "{0}").format(quoted)

    if ignore_case and data_type in ("text", "character varying", "character"):
        expression = f"lower({expression})"

    return f"{expression}::text"


def keyed_rows(database, table, key, columns):
    """{key text: {column: normalized value}} for one table."""
    schema, name = table.split(".", 1)
    key_text = " || '|' || ".join(f'"{k}"::text' for k in key)
    values = ", ".join(f"'{column}', {expression}" for column, expression in columns) or "'-', 1"
    result = rows(f'select {key_text}, json_build_object({values})::text from "{schema}"."{name}"', database)

    return {row[0]: json.loads(row[1]) for row in result}


def command_compare(arguments):
    """
    Row by row, per primary key: what only this database has, what only the other has, and which
    shared rows differ - and in which columns.

    Made for merging an older copy into a newer one (a production dump restored beside the development
    database). A timestamp is compared as the instant it names, so the same moment stored as local time
    in one database and as UTC in the other is not a difference. A column whose type differs between the
    two - an enum held as a number in one and as its name in the other - cannot be compared value by
    value; it is named once and left out, rather than counted as a difference in every row.
    """
    here_types, there_types = column_types(NAME), column_types(arguments.other)
    here_keys, there_keys = primary_keys(NAME), primary_keys(arguments.other)
    ignored = set(arguments.ignore.split(",")) if arguments.ignore else set()
    found = 0

    print(f"{'table (' + NAME + ')':44} {NAME:>9} {arguments.other:>9} {'only here':>10} {'only there':>11} {'differ':>7}")

    for mine, theirs in table_pairs(arguments, here_keys, there_keys):
        key = here_keys[mine]

        if there_keys[theirs] != key:
            print(f"{mine:44} skipped: primary key {key} here, {there_keys[theirs]} there")
            continue

        shared = [c for c in here_types[mine] if c in there_types[theirs] and c not in key and c not in ignored]
        comparable = [c for c in shared
                      if comparable_type(here_types[mine][c][0]) == comparable_type(there_types[theirs][c][0])]
        mismatched = [c for c in shared if c not in comparable]

        a = keyed_rows(NAME, mine, key,
                       [(c, normalized(c, here_types[mine][c][0], arguments.ignore_case, arguments.timezone))
                        for c in comparable])
        b = keyed_rows(arguments.other, theirs, key,
                       [(c, normalized(c, there_types[theirs][c][0], arguments.ignore_case, arguments.timezone))
                        for c in comparable])

        only_here = sorted(set(a) - set(b))
        only_there = sorted(set(b) - set(a))
        differ = sorted(k for k in set(a) & set(b) if a[k] != b[k])
        found += len(only_here) + len(only_there) + len(differ)

        label = mine if mine == theirs else f"{mine} <- {theirs}"
        print(f"{label:44} {len(a):>9} {len(b):>9} {len(only_here):>10} {len(only_there):>11} {len(differ):>7}")

        if mismatched and arguments.show:
            print(f"    not compared, type differs: {', '.join(mismatched)}")

        if arguments.show:
            if only_here:
                print(f"    only here:  {', '.join(only_here[:arguments.show])}")
            if only_there:
                print(f"    only there: {', '.join(only_there[:arguments.show])}")

            columns_changed = {}
            for k in differ:
                for column in comparable:
                    if a[k].get(column) != b[k].get(column):
                        columns_changed.setdefault(column, []).append(k)

            for column, keys in sorted(columns_changed.items(), key=lambda item: -len(item[1])):
                sample = keys[0]
                print(f"    {column}: {len(keys)} row(s), e.g. {sample}: "
                      f"{a[sample].get(column)!r} here, {b[sample].get(column)!r} there")

    sys.exit(1 if found else 0)


def command_apply(arguments):
    path = arguments.file if os.path.isabs(arguments.file) else os.path.join(REPO, arguments.file)

    if not os.path.exists(path):
        sys.stderr.write(f"refused: no such file: {path}\n")
        sys.exit(1)

    # -1 wraps the whole file in one transaction; with ON_ERROR_STOP it either lands or rolls back.
    # A script that opens its own transactions (the EF idempotent bundle does) must not be wrapped:
    # its inner COMMIT would close the outer one and the rest would run unprotected.
    arguments_for_psql = ["-f", path] if arguments.no_transaction else ["-1", "-f", path]

    for variable in arguments.var or []:
        if "=" not in variable:
            sys.stderr.write(f"refused: --var wants NAME=VALUE, got {variable!r}\n")
            sys.exit(1)

        arguments_for_psql = ["-v", variable, *arguments_for_psql]

    print(psql(arguments_for_psql, capture=True).stdout.rstrip())


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("connection", help="Show host, database, user and the .pgpass state")

    query = subparsers.add_parser("query", help="Run SQL")
    # dest is 'sql', not 'command': the subparsers themselves store into 'command', and -c would
    # otherwise overwrite the chosen subcommand with the SQL text.
    query.add_argument("-c", "--command", dest="sql", help="The SQL itself")
    query.add_argument("--file", help="Path to a .sql file (relative to the repo root)")
    query.add_argument("--format", default="tsv", choices=["tsv", "csv", "table"])

    count = subparsers.add_parser("count", help="Row counts for one or more schema.Table")
    count.add_argument("table", nargs="+")

    columns = subparsers.add_parser("columns", help="Columns of a schema.Table with their types")
    columns.add_argument("table")

    subparsers.add_parser("enum-audit",
                          help="Report enum columns still stored as integers (exit 1 if any)")

    subparsers.add_parser("active-audit",
                          help="Every IsActive column: its default, and how many rows are false or "
                               "null")

    schema_diff = subparsers.add_parser("schema-diff",
                                        help="Tables and columns that differ from another database")
    schema_diff.add_argument("other", help="The other database on the same server")
    schema_diff.add_argument("--map", action="append", metavar="HERE=THERE",
                             help="A table renamed between the two, schema.Table=schema.Table")

    compare = subparsers.add_parser("compare",
                                    help="Rows per primary key: only here, only there, differing")
    compare.add_argument("other", help="The other database on the same server")
    compare.add_argument("--map", action="append", metavar="HERE=THERE",
                         help="A table renamed between the two, schema.Table=schema.Table")
    compare.add_argument("--table", action="append", help="Only this schema.Table (repeatable)")
    compare.add_argument("--ignore", help="Columns never compared, comma-separated (audit columns, stamps)")
    compare.add_argument("--ignore-case", action="store_true", help="Compare text case-insensitively")
    compare.add_argument("--timezone", metavar="ZONE",
                         help="What a timestamp without time zone means, e.g. Europe/Berlin - one database "
                              "storing local time and the other UTC is otherwise off by the offset")
    compare.add_argument("--show", type=int, default=0, metavar="N",
                         help="List up to N keys per category and which columns differ")

    apply_script = subparsers.add_parser("apply", help="Run a .sql file as a single transaction")
    apply_script.add_argument("file")
    apply_script.add_argument("--no-transaction", action="store_true",
                              help="For scripts that open their own transactions, such as the EF "
                                   "idempotent bundle in deploy/db-init/schema.sql")
    apply_script.add_argument("--var", action="append", metavar="NAME=VALUE",
                              help="A psql variable for the script (-v), repeatable; "
                                   "what deploy/app-role.sql takes as role, owner, db and pw")

    for subparser in subparsers.choices.values():
        subparser.add_argument("--target", help="Run against a [database.targets.NAME] of project.toml "
                                                "(psql in a container over ssh) instead of the [database] host")

    arguments = parser.parse_args()

    if arguments.target:
        select_target(arguments.target)

    handlers = {
        "connection": command_connection, "query": command_query, "count": command_count,
        "columns": command_columns, "enum-audit": command_enum_audit,
        "active-audit": command_active_audit, "apply": command_apply,
        "schema-diff": command_schema_diff, "compare": command_compare,
    }

    handlers[arguments.command](arguments)


if __name__ == "__main__":
    main()
