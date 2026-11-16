#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generate all .resx files (base + satellites) from JSON translation sources.

Each Scripts/translations/{Area}.json holds an ordered map:
  "Key":        { "en": ..., "de": ..., ..., "uk": ..., "comment": optional translator note }
  "// Section": ""   -> emitted as an XML comment (section marker) in every generated file

For every area the base {Area}.resx header (everything up to the last
<resheader>) is cloned verbatim, then one <data> block per key is appended.
The English base {Area}.resx and all satellite {Area}.{lang}.resx files are
generated. Edit the JSON, re-run; the script is idempotent. Never hand-edit
the generated .resx files.

Only the base language is required. A key a satellite language does not have
yet is left out of that satellite, and the ResourceManager falls back to the
base text at runtime - one language can be worked on without the others.
"""
import glob
import json
import os
from xml.sax.saxutils import escape

import script_args
from project import project

RES = str(project.require_path("localization.resources_project"))
TRANSLATIONS = str(project.root / "Scripts" / "translations")
BASE_LANG = project.get("localization.base_language", "en")
SATELLITE_LANGS = project.get("localization.satellite_languages", [])
INDENT = "    "


def area_dir(area):
    """The folder an area's .resx files sit in: where its base file is, else the project root.

    A project may group its areas into subfolders. The base file is what places an area, so no table
    of folders has to be kept in step with the files.
    """
    for root, directories, files in os.walk(RES):
        directories[:] = [d for d in directories if d not in ("bin", "obj")]

        if f"{area}.resx" in files:
            return root

    return RES


def header_of(area):
    with open(os.path.join(area_dir(area), f"{area}.resx"), encoding="utf-8") as f:
        text = f.read()
    # Data always follows all <resheader> blocks. Split after the last one so we never cut inside the
    # commented-out example block some base files carry (which contains literal <data ...> lines).
    last = text.rindex("</resheader>")
    end = text.index("\n", last) + 1
    return text[:end]


def write_lang(area, header, lang, entries):
    # Four spaces per level, which is what Rider's rules for this repository indent XML with - and what
    # C# uses. The header above these blocks is copied out of the existing file and is indented that
    # way already, so anything else here makes the generator and the IDE take turns rewriting all 306
    # files.
    data = INDENT
    inner = INDENT * 2

    parts = [header]
    missing = 0
    for key, value in entries.items():
        if key.startswith("//"):
            parts.append(f"{data}<!-- {key[2:].strip()} -->\n")
            continue
        if not value.get(lang):
            if lang == BASE_LANG:
                raise SystemExit(f"missing {lang} for {area}/{key}")
            missing += 1
            continue
        block = (f'{data}<data name="{key}" xml:space="preserve">\n'
                 f"{inner}<value>{escape(value[lang])}</value>\n")
        if "comment" in value:
            block += f"{inner}<comment>{escape(value['comment'])}</comment>\n"
        block += f"{data}</data>\n"
        parts.append(block)
    parts.append("</root>")

    suffix = "" if lang == BASE_LANG else f".{lang}"
    out = os.path.join(area_dir(area), f"{area}{suffix}.resx")
    with open(out, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    print(f"wrote {os.path.basename(out)}" + (f" ({missing} missing, base text used)" if missing else ""))


def main():
    for path in sorted(glob.glob(os.path.join(TRANSLATIONS, "*.json"))):
        area = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)

        header = header_of(area)
        for lang in [BASE_LANG, *SATELLITE_LANGS]:
            write_lang(area, header, lang, entries)


if __name__ == "__main__":
    script_args.no_arguments(__doc__)
    main()
