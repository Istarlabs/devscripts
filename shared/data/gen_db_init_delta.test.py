#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks the seed-value comparison in gen_db_init_delta.py that needs no database.

Splitting a VALUES list is the part that can quietly go wrong: a description holds commas of its
own, a name holds an apostrophe doubled into '', and TIMESTAMPTZ brings parentheses. Get it wrong
and a column pairs up with the wrong value - which produces a syntactically fine UPDATE that writes
the wrong thing into a row nobody is watching.

    python3 Scripts/gen_db_init_delta.test.py
"""

import pathlib
import sys

sys.path[:0] = [str(folder) for folder in pathlib.Path(__file__).resolve().parents[1].iterdir() if folder.is_dir()] + [str(pathlib.Path(__file__).resolve().parents[2])]

from gen_db_init_delta import parse_insert, parse_update, split_literals

FAILURES = []


def check(name, actual, expected):
    if actual == expected:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n       expected {expected!r}\n       got      {actual!r}")
        FAILURES.append(name)


def main():
    print("split_literals:")

    check("plain values",
          split_literals("1, 'mg', TRUE, NULL"),
          ["1", "'mg'", "TRUE", "NULL"])

    check("comma inside a string",
          split_literals("1, 'Concentration - for glucose, cholesterol', 2"),
          ["1", "'Concentration - for glucose, cholesterol'", "2"])

    check("doubled quote inside a string",
          split_literals("1, 'Millilitri all''ora', 2"),
          ["1", "'Millilitri all''ora'", "2"])

    check("a doubled quote right before the closing one",
          split_literals("'a''', 'b'"),
          ["'a'''", "'b'"])

    check("parentheses of a typed literal",
          split_literals("1, TIMESTAMPTZ '2025-10-09T12:00:00Z', (1, 2), 3"),
          ["1", "TIMESTAMPTZ '2025-10-09T12:00:00Z'", "(1, 2)", "3"])

    check("comma inside a string inside parentheses",
          split_literals("(('a, b')), 'c'"),
          ["(('a, b'))", "'c'"])

    print("\nparse_insert:")

    statement = ('INSERT INTO catalog."Units" ("UnitId", "Name", "Description") '
                 "VALUES (1, 'Milligram', 'Milligram - the most common, by far') "
                 "ON CONFLICT DO NOTHING;")

    check("schema and table",
          parse_insert(statement)[:2],
          ("catalog", "Units"))

    check("columns pair with their values",
          parse_insert(statement)[2],
          {"UnitId": "1",
           "Name": "'Milligram'",
           "Description": "'Milligram - the most common, by far'"})

    check("a line that is not an INSERT",
          parse_insert('ALTER TABLE catalog."Units" ADD COLUMN "X" boolean;'),
          None)

    check("a mismatched column and value count is refused rather than paired up wrong",
          parse_insert('INSERT INTO a."B" ("One", "Two") VALUES (1);'),
          None)

    # Reading the generator's own UPDATEs back is what keeps a run idempotent: without it every run
    # rediscovers the same difference and appends the same statements again.
    print("\nparse_update:")

    check("its own statement",
          parse_update('UPDATE catalog."Units" SET "Name" = \'Milligram\' '
                       'WHERE "UnitId" = 1 AND "Name" IS NOT DISTINCT FROM \'Milligramm\';'),
          ("catalog", "Units", {"UnitId": "1"}, "Name", "'Milligram'"))

    check("a composite key",
          parse_update('UPDATE a."B" SET "C" = 2 '
                       'WHERE "K1" = 1 AND "K2" = \'x\' AND "C" IS NOT DISTINCT FROM 1;'),
          ("a", "B", {"K1": "1", "K2": "'x'"}, "C", "2")),

    check("a value holding the word AND",
          parse_update('UPDATE a."B" SET "C" = \'salt AND pepper\' '
                       'WHERE "K" = 1 AND "C" IS NOT DISTINCT FROM \'pepper\';'),
          ("a", "B", {"K": "1"}, "C", "'salt AND pepper'"))

    check("someone else's UPDATE is not ours to read",
          parse_update('UPDATE catalog."Units" SET "IsActive" = FALSE WHERE "UnitId" = 1;'),
          None)

    print()

    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        return 1

    print("all checks passed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
