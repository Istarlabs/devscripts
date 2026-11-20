#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Sort the loose files of a folder (default ~/Downloads) into category folders. Moves only, never deletes.

Not per-project: called by its path.

    python3 ~/dev/tools/devscripts/tidy.py                 dry run: the plan, nothing moves
    python3 ~/dev/tools/devscripts/tidy.py --apply         move the files, remember them for --undo
    python3 ~/dev/tools/devscripts/tidy.py --undo          put the files of the last --apply back
    python3 ~/dev/tools/devscripts/tidy.py ~/Desktop       another folder

Folders, unfinished downloads (`.part`, `.crdownload`, `.tmp`) and files younger than ten minutes stay where they
are, and so does a file whose name no rule matches. A file whose name already exists in the target folder is
skipped and reported.

The first matching rule wins. The built-in rules go by file type only. Your own rules, which usually go by name,
come first, from `~/.config/devscripts/tidy.toml` (`TIDY_CONFIG` points elsewhere); they are matched as
case-insensitive regexes against the file name:

    [[rule]]
    folder = "Finances"            # relative to the folder being tidied; "Pictures/Screenshots" is fine
    match = '^statement|invoice'

The undo log of the last --apply is `~/.local/state/devscripts/tidy-undo.json`: one --undo, not a history.
"""

import json
import os
import re
import shutil
import sys
import time
import tomllib
from collections import Counter
from pathlib import Path

CONFIG = Path(os.environ.get("TIDY_CONFIG", "~/.config/devscripts/tidy.toml")).expanduser()
UNDO_LOG = Path("~/.local/state/devscripts/tidy-undo.json").expanduser()
UNFINISHED = re.compile(r"\.(part|crdownload|tmp)$", re.I)
SETTLE_SECONDS = 600

# (folder, file-name regex). Installers come before archives so `.tar.gz` of an SDK is not guessed as a backup.
BUILTIN_RULES = [
    ("Pictures/Screenshots", r"^(screenshot|bildschirmfoto)"),
    ("Pictures", r"\.(png|jpe?g|gif|webp|svg|heic|avif)$"),
    ("Installers", r"\.(rpm|deb|appimage|flatpak|flatpakref|exe|msi|iso)$"),
    ("Archives", r"\.(zip|rar|7z|tar|tgz|tar\.(gz|xz|bz2|zst))$"),
    ("Media", r"\.(mkv|mp4|webm|avi|mov|mp3|flac|wav|ogg|m4a)$"),
    ("Documents", r"\.(pdf|docx?|odt|txt|rtf|xlsx?|csv|md)$"),
    ("Code", r"\.(cs|rs|c|cpp|h|py|php|js|ts|tsx|sql|sh)$"),
]


def load_rules(config=CONFIG):
    """The user's rules from `config` (when there is one), then the built-in ones, as (folder, compiled regex)."""
    own = []
    if config.is_file():
        with config.open("rb") as handle:
            own = [(rule["folder"], rule["match"]) for rule in tomllib.load(handle).get("rule", [])]
    return [(folder, re.compile(pattern, re.I)) for folder, pattern in own + BUILTIN_RULES]


def plan(root, rules, now=None):
    """(moves, left): moves are (source, target) paths, left the names that stay."""
    now = time.time() if now is None else now
    moves, left = [], []
    for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        settled = path.is_file() and not UNFINISHED.search(path.name) and now - path.stat().st_mtime >= SETTLE_SECONDS
        folder = next((f for f, pattern in rules if pattern.search(path.name)), None) if settled else None
        if folder:
            moves.append((path, root / folder / path.name))
        else:
            left.append(path.name)
    return moves, left


def apply(moves):
    """Move the files and write the undo log; returns the moves done. A taken target name is skipped."""
    done = []
    for source, target in moves:
        if target.exists():
            print(f"skipped, already there: {target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(source, target)
        done.append([str(source), str(target)])
    UNDO_LOG.parent.mkdir(parents=True, exist_ok=True)
    UNDO_LOG.write_text(json.dumps(done, ensure_ascii=False))
    return done


def undo():
    """Put the files of the last --apply back where they were; returns how many."""
    if not UNDO_LOG.is_file():
        sys.exit("nothing to undo")
    count = 0
    for source, target in reversed(json.loads(UNDO_LOG.read_text())):
        if Path(target).exists() and not Path(source).exists():
            shutil.move(target, source)
            count += 1
    return count


def main():
    arguments = sys.argv[1:]
    if "--undo" in arguments:
        print(f"moved back: {undo()}")
        return
    folders = [a for a in arguments if not a.startswith("--")]
    root = Path(folders[0] if folders else "~/Downloads").expanduser()
    if not root.is_dir():
        sys.exit(f"not a folder: {root}")
    moves, left = plan(root, load_rules())
    for folder, count in sorted(Counter(t.parent.relative_to(root).as_posix() for _, t in moves).items()):
        print(f"{count:4}  {folder}")
    print(f"stay: {len(left)}")
    if "--apply" in arguments:
        print(f"moved: {len(apply(moves))}")
    else:
        print("dry run; --apply moves them")


if __name__ == "__main__":
    main()
