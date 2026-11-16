#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""A KDE service menu (.desktop) from a small TOML definition.

    python3 servicemenu.py dolphin/av1-convert.menu.toml [--bin ~/.local/bin]

prints the .desktop file. `rust.py install` calls the same function for every definition listed in
[rust.service_menu], so the finished file lives nowhere but where it is installed.

    mime    = ["video/*"]
    submenu = { en = "Video Converter", de = "Video-Konverter" }   # or a plain string
    # priority = "TopLevel"                                        # the default

    [[action]]
    id   = "convert"                                   # the section name; unique, no spaces
    name = { en = "Convert to AV1", de = "Zu AV1 konvertieren" }
    icon = "video-x-generic"                           # optional
    exec = "{bin}/av1-convert --notify %F"             # {bin} is the directory the binary was installed to

    [[action]]
    separator = true                                   # an empty entry, as a line between groups

The `en` text becomes `Name=`, every other language `Name[xx]=`. The actions appear in the order
they are written.
"""

import argparse
import sys
import tomllib
from pathlib import Path


def localized(key, value, where):
    """`Key=` for a plain string or the `en` text, `Key[xx]=` for the other languages."""
    if isinstance(value, str):
        return [f"{key}={value}"]

    if not isinstance(value, dict) or "en" not in value:
        raise ValueError(f"{where}: {key} needs a string or a table with an `en` text")

    lines = [f"{key}={value['en']}"]
    lines += [f"{key}[{language}]={words}" for language, words in value.items() if language != "en"]

    return lines


def render(definition, bin_dir, source="a definition"):
    """The .desktop text for one parsed definition. Raises ValueError for what cannot be made of it."""
    mime = definition.get("mime")

    if not mime or not isinstance(mime, list):
        raise ValueError(f"{source}: mime must be a list such as [\"video/*\"]")

    actions = definition.get("action") or []

    if not actions:
        raise ValueError(f"{source}: there is no [[action]]")

    names, sections, separators = [], [], 0

    for position, action in enumerate(actions, start=1):
        where = f"{source}, action {position}"

        if action.get("separator"):
            separators += 1
            name = f"separator{separators}"
            sections.append(["", f"[Desktop Action {name}]", "Name="])
            names.append(name)

            continue

        name = action.get("id")

        if not name or " " in name or name in names:
            raise ValueError(f"{where}: id must be given once, without spaces ({name!r})")

        if not action.get("exec"):
            raise ValueError(f"{where}: exec is missing")

        lines = ["", f"[Desktop Action {name}]", *localized("Name", action.get("name"), where)]

        if action.get("icon"):
            lines.append(f"Icon={action['icon']}")

        lines.append(f"Exec={action['exec'].replace('{bin}', str(bin_dir))}")
        sections.append(lines)
        names.append(name)

    header = [
        f"# Generated from {source} by devscripts/shared/lib/servicemenu.py - edit the definition, not this file.",
        "[Desktop Entry]",
        "Type=Service",
        "X-KDE-ServiceTypes=KonqPopupMenu/Plugin",
        f"MimeType={';'.join(mime)};",
        f"Actions={';'.join(names)};",
        f"X-KDE-Priority={definition.get('priority', 'TopLevel')}",
    ]

    if "submenu" in definition:
        header += localized("X-KDE-Submenu", definition["submenu"], source)

    return "\n".join(header + [line for section in sections for line in section]) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("definition", type=Path)
    parser.add_argument("--bin", default="~/.local/bin", help="What {bin} stands for in an exec line")
    arguments = parser.parse_args()

    try:
        definition = tomllib.loads(arguments.definition.read_text(encoding="utf-8"))
        sys.stdout.write(render(definition, Path(arguments.bin).expanduser(), arguments.definition.name))
    except (OSError, ValueError) as problem:  # tomllib.TOMLDecodeError is a ValueError
        sys.exit(f"{arguments.definition}: {problem}")


if __name__ == "__main__":
    main()
