#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks that find.py --tag reads a Razor tag the way Razor does.

The thing worth proving is that a `>` inside an attribute value does not end the tag, because a tag
cut in the middle of a lambda reports every attribute behind it as missing.

    python3 find.test.py
"""

import importlib.util
import re
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent

# The shared scripts import `project`, which reads the project.toml of the repository it runs in.
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))

specification = importlib.util.spec_from_file_location("find_script", HERE / "find.py")
find = importlib.util.module_from_spec(specification)
specification.loader.exec_module(find)

FAILURES = []


def check(name, actual, expected):
    if actual == expected:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n       expected {expected!r}\n       got      {actual!r}")
        FAILURES.append(name)


def tags(text, tag="FluentButton"):
    return [(number, names) for number, _, names, _ in find.razor_tags(text, tag)]


def empties(text, tag="FluentButton"):
    return [empty for _, _, _, empty in find.razor_tags(text, tag)]


def main():
    check("a plain tag lists its attributes, a boolean one included",
          tags('<FluentButton Title="Edit" IconOnly="true" Disabled/>'),
          [(1, {"title", "icononly", "disabled"})])

    check("a lambda in a value does not end the tag",
          tags('<FluentButton OnClick="@(() => Save(item))" Title="Save"/>'),
          [(1, {"onclick", "title"})])

    check("a nested interpolated string does not end the tag",
          tags('<FluentButton Content="@($"{a}: {b} > {c}")" Title="x"/>'),
          [(1, {"content", "title"})])

    check("a tag over several lines is one tag, and its first line is the line number",
          tags('<div>\n<FluentButton\n    IconStart="@(new Icon())"\n    OnClick="@(() => Go())"\n    Title="Go"/>\n</div>'),
          [(2, {"iconstart", "onclick", "title"})])

    check("a tag without a title says so",
          tags('<FluentButton\n  IconStart="@(new Icon())"\n  OnClick="@(() => Go())"/>'),
          [(1, {"iconstart", "onclick"})])

    check("another component with the same prefix is not the tag",
          tags('<FluentButtonGroup Title="x"></FluentButtonGroup><FluentButton Title="y"/>'),
          [(1, {"title"})])

    check("a razor directive attribute counts as an attribute",
          tags('<FluentButton @onclick="Save" @bind-Value="x" Title="t"/>'),
          [(1, {"@onclick", "@bind-value", "title"})])

    check("a char literal holding a quote does not open a string",
          tags('<FluentButton OnClick="@(() => Pick(\'"\'))" Title="t"/>'),
          [(1, {"onclick", "title"})])

    check("two tags are two entries, each with its own attributes",
          tags('<FluentButton Title="a"/>\n<FluentButton IconOnly/>'),
          [(1, {"title"}), (2, {"icononly"})])

    check("a self-closing tag and one closed at once hold nothing, one with a label does",
          empties('<FluentButton Title="a"/><FluentButton Title="b">\n  </FluentButton><FluentButton Title="c">Save</FluentButton>'),
          [True, True, False])

    check("a tag with markup inside holds something",
          empties('<FluentButton><FluentIcon/></FluentButton>'),
          [False])

    def declares(profile, symbol, line):
        pattern = find.PROFILES[profile]["declaration"].replace("{symbol}", symbol)
        return re.search(pattern, line) is not None

    check("cpp: a class and an out-of-line definition declare the symbol, a call does not",
          [declares("cpp", "Terminal", "class Terminal : public QObject {"),
           declares("cpp", "start", "void Terminal::start() {"),
           declares("cpp", "start", "    terminal.start();"),
           declares("cpp", "Terminal", "class Terminal;")],
          [True, True, False, False])

    forward = re.compile(find.PROFILES["cpp"]["forward"].replace("{symbol}", "Terminal"))

    check("cpp --forward: `class X;` and `enum class X : int;` match, a definition does not",
          [bool(forward.search(line)) for line in ("class Terminal;", "enum class Terminal : int;",
                                                   "class Terminal {", "Terminal t;")],
          [True, True, False, False])

    check("python and rust: def/fn declare, a call does not",
          [declares("python", "run", "def run(self):"), declares("python", "run", "    run()"),
           declares("rust", "run", "pub fn run() {")],
          [True, False, True])

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) FAILED")
        return 1

    print("\nall checks passed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
