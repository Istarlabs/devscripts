#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""One entry point for reading the application's own log.

NLog writes to `logs/${shortdate}/app.log` *relative to the app's working directory*, which is the
build output (`{app_project}/bin/Debug/net10.0/`) and not the project folder — so the path is the
part everyone gets wrong, and a hand-built `tail`/`grep` line is refused by this repository's
tooling rule anyway.

    python3 Scripts/logs.py                     the last 50 entries of today
    python3 Scripts/logs.py -n 200              more of them
    python3 Scripts/logs.py --all               all of them
    python3 Scripts/logs.py --date 2026-08-19   another day
    python3 Scripts/logs.py --grep ega          entries whose text contains this (case-insensitive)
    python3 Scripts/logs.py --level Warn        this level and above
    python3 Scripts/logs.py --list              which days exist, with size and entry count
    python3 Scripts/logs.py --path              just the file this would read
    python3 Scripts/logs.py --android           the app's logcat on the phone (adb), same -n/--grep/--level

Filtering works on *entries*, not lines: a logged exception spans many lines, and the ones after the
first carry no timestamp. Matching them line by line would print a stack trace without the message
that explains it, or the message without its trace.

The application project is logs.app_project in Scripts/project.toml. The log directory is found
under its `bin/*/*/`, named after `NLog.variables.logdir` in appsettings.json, so a renamed target
does not silently produce an empty answer — it produces a refusal. {PREFIX}_LOG_DIR overrides the
lot for an app running somewhere else.

--android reads `adb logcat` of the MAUI app instead of a file: its process only while the app runs, the whole
buffer when it does not (a crash is in there, under another pid). Needs the [android] block, see android.py.

Exit code is non-zero when the day asked for has no log, so it can gate a chain.
"""
import argparse
import glob
import json
import os
import re
import sys

import android
from project import project

REPO = str(project.root)
# logs.app_project is looked up when a log file is read, not on import: --android needs none.

# NLog's own order. `--level Warn` means Warn and everything worse, which is the only reading of
# "show me the warnings" that does not hide the errors underneath them.
LEVELS = ["Trace", "Debug", "Info", "Warn", "Error", "Fatal"]

# The layout starts every entry with "[2026-08-20 19:23:48.0720] INFO - ". A line that does not
# match is a continuation of the entry above it.
ENTRY_START = re.compile(r"^\[(?P<time>[^\]]+)\]\s+(?P<level>[A-Z]+)\s+-")


def fail(message):
    sys.stderr.write(f"refused: {message}\n")
    sys.exit(1)


def app_directory():
    return str(project.require_path("logs.app_project"))


def log_directory_name():
    """
    The directory NLog writes into, read out of appsettings.json rather than assumed.

    Returns the name only ("logs"): the `${shortdate}` below it is a directory per day, which is
    what makes `--date` and `--list` possible in the first place.
    """
    appsettings = os.path.join(app_directory(), "appsettings.json")

    if not os.path.exists(appsettings):
        fail(f"no appsettings.json at {appsettings}")

    with open(appsettings, encoding="utf-8") as handle:
        settings = json.load(handle)

    configured = settings.get("NLog", {}).get("variables", {}).get("logdir", "")
    base = configured.split("${")[0].strip("/")

    if not base:
        fail(f"NLog.variables.logdir in appsettings.json is {configured!r}, "
             "which names no directory this script can find")

    return base


def log_root():
    """
    Where the days live, or a refusal naming what was searched.

    Several build outputs can carry one (Debug and Release), so the newest wins — that is the one
    the app most recently ran from.
    """
    override = project.env("LOG_DIR")

    if override:
        if not os.path.isdir(override):
            fail(f"{project.env_name('LOG_DIR')} points at {override}, which is not a directory")

        return override

    output_glob = os.path.join(app_directory(), "bin", "*", "*")
    candidates = [path for path in glob.glob(os.path.join(output_glob, log_directory_name()))
                  if os.path.isdir(path)]

    if not candidates:
        fail(f"no log directory under {output_glob} — has the app been run from this checkout?")

    return max(candidates, key=os.path.getmtime)


def days(root):
    return sorted(entry for entry in os.listdir(root) if os.path.isdir(os.path.join(root, entry)))


def log_file(root, date):
    """The log of one day, or of the most recent day that has one when no date was asked for."""
    available = days(root)

    if not available:
        fail(f"{root} holds no day yet")

    if date is None:
        date = available[-1]
    elif date not in available:
        fail(f"no log for {date} — {root} holds {', '.join(available)}")

    path = os.path.join(root, date, "app.log")

    if not os.path.exists(path):
        fail(f"{path} does not exist")

    return path


def read_entries(path):
    """
    The file as entries: each is its first line plus every continuation line under it.

    A file that starts mid-entry (a rotated or truncated log) would otherwise drop its opening
    lines, so anything before the first timestamp becomes an entry of its own, at no level.
    """
    entries = []

    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            match = ENTRY_START.match(line)

            if match or not entries:
                entries.append({"level": match.group("level").title() if match else "",
                                "lines": [line]})
            else:
                entries[-1]["lines"].append(line)

    return entries


# `adb logcat -v threadtime`: "08-20 19:23:48.072  1234  1250 I Tag: message".
LOGCAT_ENTRY = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d+\s+\d+\s+\d+\s+(?P<level>[VDIWEFA])\s")
LOGCAT_LEVELS = {"V": "Trace", "D": "Debug", "I": "Info", "W": "Warn", "E": "Error", "F": "Fatal", "A": "Fatal"}


def android_entries():
    """The phone's logcat as entries in read_entries' shape: the app's process while it runs, else everything."""
    android.require_device()
    pid = android.adb("shell", "pidof", "-s", android.application_id(), check=False).stdout.strip()
    command = ["logcat", "-d", "-v", "threadtime"] + ([f"--pid={pid}"] if pid else [])

    if not pid:
        print(f"{android.application_id()} is not running - showing the whole logcat buffer", file=sys.stderr)

    entries = []

    for line in android.adb(*command).stdout.splitlines():
        match = LOGCAT_ENTRY.match(line)

        if match or not entries:
            entries.append({"level": LOGCAT_LEVELS[match.group("level")] if match else "", "lines": [line]})
        else:
            entries[-1]["lines"].append(line)

    return entries


def matching(entries, term, level):
    if level:
        minimum = LEVELS.index(level)
        # An entry whose level this script does not know stays in: a log it cannot classify is the
        # last thing to hide from someone who came here looking for a problem.
        entries = [entry for entry in entries
                   if entry["level"] not in LEVELS or LEVELS.index(entry["level"]) >= minimum]

    if term:
        needle = term.lower()
        entries = [entry for entry in entries
                   if any(needle in line.lower() for line in entry["lines"])]

    return entries


def command_list(root):
    for day in days(root):
        path = os.path.join(root, day, "app.log")

        if not os.path.exists(path):
            print(f"{day}  (no app.log)")
            continue

        size = os.path.getsize(path)
        count = len(read_entries(path))
        print(f"{day}  {count:>6} entries  {size / 1024:>8.1f} KiB")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="Day to read, as 2026-08-19 (default: the most recent one)")
    parser.add_argument("-n", "--lines", type=int, default=50,
                        help="How many entries to show (default: 50)")
    parser.add_argument("--all", action="store_true", help="Show every matching entry")
    parser.add_argument("--grep", help="Only entries containing this text, case-insensitive")
    parser.add_argument("--level", choices=LEVELS, help="This level and above")
    parser.add_argument("--list", action="store_true", help="Which days exist, with their size")
    parser.add_argument("--path", action="store_true", help="Print the file that would be read")
    parser.add_argument("--android", action="store_true", help="Read the MAUI app's logcat over adb instead of a file")

    arguments = parser.parse_args()

    if arguments.android:
        if arguments.list or arguments.path or arguments.date:
            fail("--list, --path and --date are about the log files; logcat has none")

        path, entries = "logcat", matching(android_entries(), arguments.grep, arguments.level)
    else:
        root = log_root()

        if arguments.list:
            command_list(root)
            return

        path = log_file(root, arguments.date)

        if arguments.path:
            print(path)
            return

        entries = matching(read_entries(path), arguments.grep, arguments.level)

    if not entries:
        print(f"{path}: nothing matches", file=sys.stderr)
        return

    shown = entries if arguments.all else entries[-arguments.lines:]
    hidden = len(entries) - len(shown)

    # Ahead of the entries, not after them: stderr and stdout interleave by buffer, so a note
    # printed last lands anywhere, and as a header it reads as what it is.
    if hidden:
        print(f"… {hidden} earlier matching entr{'y' if hidden == 1 else 'ies'} not shown, "
              f"of {len(entries)}\n", file=sys.stderr)

    for entry in shown:
        print("\n".join(entry["lines"]))


if __name__ == "__main__":
    main()
