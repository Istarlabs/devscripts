#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""The gettext half of localization: the .pot, the .po files, and what is still untranslated.

`translations.py` is the .NET side of the same job - .resx generated out of JSON. A Qt/KDE program
does not work that way: the English text *is* the `i18n()` argument, and a translation is a .po file
keyed by it. Nothing to generate, so nothing the two could share beyond the word.

    python3 Scripts/i18n.py extract            rebuild po/<domain>.pot from the sources
    python3 Scripts/i18n.py update [de ...]    extract, then merge it into each .po
    python3 Scripts/i18n.py add fr             start a language, from the current .pot
    python3 Scripts/i18n.py status             translated / fuzzy / untranslated, per language

What this is really for is the `xgettext` call. KDE's i18n() family is twenty-odd functions -
i18n, i18nc, i18np, ki18n, xi18n and each one's domain-taking twin - and xgettext finds a string
only if the call it sits in is named with a `-k` flag. Miss one and those strings silently never
reach a translator. The list below is that call, written down once.

`ki18n_install(po)` in CMakeLists.txt does the rest: it compiles every .po to a .mo at build time
and installs it where KLocalizedString looks. There is deliberately no `compile` verb here - a .mo
in the source tree would be a build artefact nobody asked for.

Configuration, in `Scripts/project.toml`. Its own block rather than `[localization]`, which is how
install.py tells the two worlds apart - a .NET project has no use for a shim of this:

    [i18n]
    domain = "kurodlg"          # the .pot/.po basename; the same name setApplicationDomain() takes
    languages = ["de"]          # what to carry, without the source language
    flavour = "kde"             # optional: "kde" (the default) or "rust"
    sources = ["src"]           # optional, defaults to src/
    po_dir = "po"               # optional
    bugs_address = "..."        # optional, goes into the .pot header

`flavour = "rust"` reads .rs files instead, with xgettext's own Rust parser - gettext 0.26 knows the
gettext-rs family by name, so there is no keyword list to keep. A Rust crate also has no
`ki18n_install(po)`: `rust.py install` compiles the .po files and puts the .mo where gettext looks.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from project import project

REPO = project.root

DOMAIN = project.require("i18n.domain")
LANGUAGES = project.get("i18n.languages", [])
SOURCES = project.get("i18n.sources", ["src"])
PO_DIR = REPO / project.get("i18n.po_dir", "po")
POT = PO_DIR / f"{DOMAIN}.pot"

HOLDER = project.get("spdx.holder", "")
BUGS_ADDRESS = project.get("i18n.bugs_address", "")

FLAVOUR = project.get("i18n.flavour", "kde")

# This machine's gettext speaks German, and `status` reads msgfmt's counts back out of its output.
ENV = {**os.environ, "LC_ALL": "C"}

# Every spelling of a translated string KDE offers. The plain family, the ki18n deferred family, the
# xi18n markup family, and each one's `d` twin that takes the domain as its first argument - hence
# the shifted positions. `1c,2` means "argument 1 is the disambiguating context, 2 is the text".
KDE_KEYWORDS = [
    "i18n:1", "i18nc:1c,2", "i18np:1,2", "i18ncp:1c,2,3",
    "i18nd:2", "i18ndc:2c,3", "i18ndp:2,3", "i18ndcp:2c,3,4",
    "ki18n:1", "ki18nc:1c,2", "ki18np:1,2", "ki18ncp:1c,2,3",
    "ki18nd:2", "ki18ndc:2c,3", "ki18ndp:2,3", "ki18ndcp:2c,3,4",
    "xi18n:1", "xi18nc:1c,2", "xi18np:1,2", "xi18ncp:1c,2,3",
    "xi18nd:2", "xi18ndc:2c,3", "xi18ndp:2,3", "xi18ndcp:2c,3,4",
    "kxi18n:1", "kxi18nc:1c,2", "kxi18np:1,2", "kxi18ncp:1c,2,3",
    "kxi18nd:2", "kxi18ndc:2c,3", "kxi18ndp:2,3", "kxi18ndcp:2c,3,4",
    "I18N_NOOP:1", "I18NC_NOOP:1c,2",
    "tr2i18n:1", "tr2xi18n:1",
]

# What a flavour reads, and how xgettext is to read it. Rust needs no keyword list: gettext 0.26
# parses Rust itself and its defaults already name gettext-rs' functions, which is the whole
# difference to the KDE side - there the twenty-odd spellings of i18n() have to be spelled out.
FLAVOURS = {
    "kde": ((".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx", ".qml"),
            ["-C", "--kde"] + [f"--keyword={keyword}" for keyword in KDE_KEYWORDS]),
    "rust": ((".rs",), ["--language=Rust"]),
}

if FLAVOUR not in FLAVOURS:
    sys.exit(f"refused: i18n.flavour is {FLAVOUR!r}, known are {', '.join(sorted(FLAVOURS))}.")

SOURCE_SUFFIXES, XGETTEXT_FLAGS = FLAVOURS[FLAVOUR]


def refuse(message):
    sys.stderr.write(f"refused: {message}\n")
    sys.exit(2)


def require_tools(*names):
    missing = [name for name in names if shutil.which(name) is None]

    if missing:
        refuse(f"{', '.join(missing)} not installed - this needs the gettext tools.")


def run(command):
    return subprocess.run(command, cwd=REPO, env=ENV, capture_output=True, text=True,
                          errors="replace")


def po_file(language):
    return PO_DIR / language / f"{DOMAIN}.po"


def source_files():
    """Every translatable source, in a stable order so the .pot does not reshuffle between runs."""
    found = []

    for source in SOURCES:
        root = REPO / source

        if not root.is_dir():
            refuse(f"{root} does not exist - check i18n.sources in {project.config_file}.")

        found += [path for path in root.rglob("*") if path.suffix in SOURCE_SUFFIXES]

    return sorted(str(path.relative_to(REPO)) for path in found)


def without_creation_date(text):
    """The .pot minus its timestamp, which is the only thing a re-extraction usually changes."""
    return [line for line in text.splitlines() if not line.startswith('"POT-Creation-Date:')]


def extract(quiet=False):
    require_tools("xgettext")
    files = source_files()

    if not files:
        refuse(f"no {'/'.join(SOURCE_SUFFIXES)} files under {', '.join(SOURCES)}.")

    PO_DIR.mkdir(parents=True, exist_ok=True)
    written = PO_DIR / f"{DOMAIN}.pot.new"

    command = [
        "xgettext",
        "--from-code=UTF-8",
        "-ci18n",  # a `// i18n: ...` comment above a call is a note to the translator
        "--sort-by-file",
        f"--package-name={project.name}",
        f"--output={written}",
    ] + XGETTEXT_FLAGS

    if HOLDER:
        command.append(f"--copyright-holder={HOLDER}")

    if BUGS_ADDRESS:
        command.append(f"--msgid-bugs-address={BUGS_ADDRESS}")

    result = run(command + files)

    if result.returncode != 0:
        written.unlink(missing_ok=True)
        sys.stderr.write(result.stderr)
        sys.exit(1)

    # Rewriting the file when only its timestamp moved would put a diff in front of anyone who runs
    # this, and a diff that says nothing is one people learn to commit without reading.
    if POT.is_file() and without_creation_date(POT.read_text()) == without_creation_date(written.read_text()):
        written.unlink()

        if not quiet:
            print(f"  {POT.relative_to(REPO)} unchanged, {statistics(POT)}")

        return False

    written.replace(POT)

    if not quiet:
        print(f"  {POT.relative_to(REPO)} written, {statistics(POT)}")

    return True


def merge(languages):
    require_tools("msgmerge")
    failed = False

    for language in languages:
        target = po_file(language)

        if not target.is_file():
            sys.stderr.write(f"  {language}: no {target.relative_to(REPO)} - "
                             f"`i18n.py add {language}` starts one\n")
            failed = True
            continue

        # --previous leaves the old msgid on a string that became fuzzy, which is what makes a
        # changed sentence reviewable rather than a retranslation from nothing.
        result = run(["msgmerge", "--update", "--backup=none", "--previous", "--quiet",
                      str(target), str(POT)])

        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            failed = True
            continue

        print(f"  {target.relative_to(REPO)}  {statistics(target)}")

    return not failed


def statistics(path):
    """Translated/fuzzy/untranslated, counted by msgfmt.

    Counting `msgid` lines here would be wrong, and quietly: an entry whose text is long enough to
    wrap is written as `msgid ""` followed by the pieces, and is indistinguishable from the header.
    """
    require_tools("msgfmt")
    result = run(["msgfmt", "--statistics", "--check-format", "-o", os.devnull, str(path)])

    # msgfmt reports the counts on stderr and says nothing at all about a category that is empty.
    return result.stderr.strip().replace("\n", "; ") or "no messages"


def add(language):
    require_tools("msginit")

    if not POT.is_file():
        extract(quiet=True)

    target = po_file(language)

    if target.is_file():
        refuse(f"{target.relative_to(REPO)} already exists.")

    target.parent.mkdir(parents=True, exist_ok=True)
    result = run(["msginit", "--no-translator", f"--locale={language}",
                  f"--input={POT}", f"--output-file={target}"])

    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(1)

    print(f"  {target.relative_to(REPO)} created")

    if language not in LANGUAGES:
        print(f"  add {language!r} to i18n.languages in "
              f"{project.config_file.relative_to(REPO)}")


def status():
    if not POT.is_file():
        refuse(f"{POT.relative_to(REPO)} does not exist - run `i18n.py extract`.")

    print(f"{POT.relative_to(REPO)}: {statistics(POT)}")

    if not LANGUAGES:
        print("  no languages configured (i18n.languages)")
        return

    for language in LANGUAGES:
        target = po_file(language)

        if not target.is_file():
            print(f"  {language}: missing")
            continue

        print(f"  {language}: {statistics(target)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)

    actions.add_parser("extract", help="rebuild the .pot from the sources")

    updating = actions.add_parser("update", help="extract, then merge into each .po")
    updating.add_argument("languages", nargs="*", help="default: every configured language")
    updating.add_argument("--no-extract", action="store_true",
                          help="merge the .pot as it stands")

    starting = actions.add_parser("add", help="start a new language from the .pot")
    starting.add_argument("language")

    actions.add_parser("status", help="what is translated, per language")

    arguments = parser.parse_args()

    if arguments.action == "extract":
        print("Extracting ...")
        extract()
    elif arguments.action == "update":
        if not arguments.no_extract:
            print("Extracting ...")
            extract()

        languages = arguments.languages or LANGUAGES

        if not languages:
            refuse(f"no languages: pass one, or set i18n.languages in "
                   f"{project.config_file}.")

        print("Merging ...")

        if not merge(languages):
            sys.exit(1)
    elif arguments.action == "add":
        add(arguments.language)
    elif arguments.action == "status":
        status()


if __name__ == "__main__":
    main()
