#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks for servicemenu.py: python3 servicemenu.test.py"""

import configparser
import importlib.util
import tomllib
from pathlib import Path

spec = importlib.util.spec_from_file_location("servicemenu", Path(__file__).with_name("servicemenu.py"))
servicemenu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(servicemenu)

DEFINITION = """
mime = ["video/*", "image/*"]
submenu = { en = "Converter", de = "Konverter" }

[[action]]
id = "convert"
name = { en = "Convert", de = "Konvertieren" }
icon = "video-x-generic"
exec = "{bin}/tool --notify %F"

[[action]]
separator = true

[[action]]
id = "plain"
name = "Plain name"
exec = "konsole -e {bin}/tool %f"
"""


def parse(text):
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    parser.optionxform = str
    parser.read_string(text)

    return parser


def render(text):
    return servicemenu.render(tomllib.loads(text), "/opt/bin", "test.menu.toml")


def raises(text, fragment):
    try:
        render(text)
    except ValueError as problem:
        assert fragment in str(problem), f"{fragment!r} not in {problem}"

        return

    raise AssertionError(f"no error for {fragment!r}")


entry = parse(render(DEFINITION))

assert entry["Desktop Entry"]["MimeType"] == "video/*;image/*;"
assert entry["Desktop Entry"]["Actions"] == "convert;separator1;plain;", "separators take part, in order"
assert entry["Desktop Entry"]["X-KDE-Priority"] == "TopLevel"
assert entry["Desktop Entry"]["X-KDE-Submenu"] == "Converter"
assert entry["Desktop Entry"]["X-KDE-Submenu[de]"] == "Konverter"
assert entry["Desktop Action convert"]["Name[de]"] == "Konvertieren"
assert entry["Desktop Action convert"]["Icon"] == "video-x-generic"
assert entry["Desktop Action convert"]["Exec"] == "/opt/bin/tool --notify %F", "{bin} is filled, %F is not"
assert entry["Desktop Action separator1"]["Name"] == ""
assert entry["Desktop Action plain"]["Name"] == "Plain name"
assert "Icon" not in entry["Desktop Action plain"]
assert render(DEFINITION).startswith("# Generated from test.menu.toml"), "the header says where it came from"

raises(DEFINITION.replace('id = "plain"', 'id = "convert"'), "id must be given once")
raises(DEFINITION.replace('exec = "konsole -e {bin}/tool %f"', ""), "exec is missing")
raises(DEFINITION.replace('name = { en = "Convert", de = "Konvertieren" }', 'name = { de = "Nur" }'), "`en` text")
raises(DEFINITION.replace('mime = ["video/*", "image/*"]', ""), "mime")
raises('mime = ["a/b"]', "no [[action]]")

print("servicemenu: ok")
