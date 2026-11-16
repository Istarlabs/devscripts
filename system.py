#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Asking this machine what is going on: the journal, systemd units, processes. Read-only.

The local counterpart of `ops/server.py`, for the commands that are needed too rarely to remember.
Not per-project: called by its path.

    python3 ~/dev/tools/devscripts/system.py logs sshd                    a unit: last 60 entries of the last 6 hours
    python3 ~/dev/tools/devscripts/system.py logs --process steam --grep "error|fail" --exclude "vulkan|shader"
    python3 ~/dev/tools/devscripts/system.py logs --kernel --grep "usb|xpad" --boot
    python3 ~/dev/tools/devscripts/system.py logs --priority err --since "-2h"     everything at error level or worse
    python3 ~/dev/tools/devscripts/system.py logs plasma-kwin_wayland --boot -1 -n 200     the boot before this one

    system.py failed                    units that failed, system and user
    system.py units PATTERN             which units have PATTERN in their name, and their state
    system.py status UNIT | cat UNIT    what systemd says; `cat` shows the unit file with its drop-ins
    system.py timers | boots            the timers; the boots the journal still holds (for `logs --boot N`)
    system.py ps PATTERN                processes matching a regex, with the unit each belongs to
    system.py blame [-n 15]             how long the boot took, and the slowest units

`logs` narrows in this order: where from (a UNIT, `--process NAME`, `--kernel`, or everything), when (`--since`,
or `--boot` for a whole boot: no number is this boot, -1 the one before), how bad (`--priority`), then `--grep` /
`--exclude` as case-insensitive regexes. The filters work on *entries*, as in `server.py`: a traceback comes back
whole. Exit 1 when nothing matches.

A unit is looked up as a user unit first (`systemctl --user`), then as a system unit, so the scope does not have
to be known; `--user` / `--system` force one. `--process` matches the program's name, both as the kernel knows
it (cut to 15 characters) and as it tags its own log lines.

Nothing here changes anything, so nothing needs sudo. Starting or stopping a unit stays `systemctl` by hand.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "ops"))
from server import entries, select

PRIORITIES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")


def run(command, check=True):
    """Run a local command; exit with its error unless `check` is off, then return stdout and stderr."""
    result = subprocess.run(command, capture_output=True, text=True)
    if check and result.returncode != 0:
        sys.exit(result.stderr.strip() or f"{' '.join(command)} failed with {result.returncode}")
    return result.stdout + ("" if check else result.stderr)


def scope(arguments):
    """["--user"] or []: what was forced, else whether systemd knows the unit as a user unit."""
    if arguments.user or arguments.system:
        return ["--user"] if arguments.user else []
    state = run(["systemctl", "--user", "show", "-p", "LoadState", "--value", arguments.unit], check=False).strip()
    return [] if state in ("", "not-found") else ["--user"]


def journal_command(arguments, user):
    """The journalctl call for `logs`; `user` is scope()'s answer. Options are ANDed by journalctl."""
    command = ["journalctl", "--no-pager", *user]
    if arguments.kernel:
        command.append("-k")
    if arguments.unit:
        command += ["-u", arguments.unit]
    if arguments.boot is not None:
        command += ["-b", str(arguments.boot)]
    elif not arguments.kernel:
        command += ["--since", arguments.since]
    if arguments.priority:
        command += ["-p", arguments.priority]
    if arguments.process:
        # `+` is journalctl's OR: _COMM is the kernel's name for the process, cut to 15 characters.
        command += [f"_COMM={arguments.process[:15]}", "+", f"SYSLOG_IDENTIFIER={arguments.process}"]
    return command


def logs(arguments):
    user = scope(arguments) if arguments.unit else (["--user"] if arguments.user else [])
    chosen = select(entries(run(journal_command(arguments, user)).splitlines()), arguments.grep, arguments.exclude)
    for entry in chosen[-arguments.lines:]:
        for line in entry:
            print(line if len(line) <= arguments.width else line[: arguments.width] + " ...")
    if not chosen:
        sys.exit(1)


def both_scopes(*command):
    """Print a systemctl listing for the system and for the user manager."""
    for title, flag in (("system", []), ("user", ["--user"])):
        print(f"--- {title}")
        print(run(["systemctl", *flag, *command, "--no-pager"], check=False), end="")


def failed(_):
    both_scopes("--failed", "--no-legend")


def units(arguments):
    # without --type the .device units of every matching sysfs path drown the answer
    both_scopes("list-units", "--all", "--no-legend", "--type=service,socket,timer,target,path,mount,scope",
                f"*{arguments.pattern}*")


def timers(_):
    both_scopes("list-timers")


def status(arguments):
    print(run(["systemctl", *scope(arguments), "status", "--no-pager", arguments.unit], check=False), end="")


def cat(arguments):
    print(run(["systemctl", *scope(arguments), "cat", "--no-pager", arguments.unit]), end="")


def boots(_):
    print(run(["journalctl", "--list-boots", "--no-pager"]), end="")


def ps(arguments):
    own = str(os.getpid())
    rows = []
    for line in run(["ps", "-eo", "pid=,user=,pcpu=,pmem=,etime=,unit=,uunit=,args=", "-ww"]).splitlines():
        pid, user, cpu, memory, elapsed, unit, user_unit, program = line.split(None, 7)
        if pid != own and re.search(arguments.pattern, line, re.IGNORECASE):
            # a process of the user session sits in user@1000.service; its user unit is the one that says something
            rows.append((pid, user, cpu, memory, elapsed, unit if user_unit == "-" else user_unit, program))
    for row in [("PID", "USER", "%CPU", "%MEM", "ELAPSED", "UNIT", "COMMAND"), *rows]:
        line = "{:>7} {:<10} {:>5} {:>5} {:>11}  {}  {}".format(*row)
        print(line if len(line) <= arguments.width else line[: arguments.width] + " ...")
    if not rows:
        sys.exit(1)


def blame(arguments):
    print(run(["systemd-analyze"]), end="")
    print("\n".join(run(["systemd-analyze", "blame", "--no-pager"]).splitlines()[: arguments.lines]))


def main():
    parser = argparse.ArgumentParser(prog="system.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def unit_command(name, run_it, unit_optional=False):
        sub = commands.add_parser(name)
        sub.add_argument("unit", nargs="?" if unit_optional else None)
        which = sub.add_mutually_exclusive_group()
        which.add_argument("--user", action="store_true", help="the user manager, without looking the unit up")
        which.add_argument("--system", action="store_true", help="the system manager, without looking the unit up")
        sub.set_defaults(run=run_it)
        return sub

    sub = unit_command("logs", logs, unit_optional=True)
    sub.add_argument("--process", metavar="NAME", help="entries of this program, whatever unit it runs in")
    sub.add_argument("--kernel", action="store_true", help="the kernel log (dmesg) of this boot, or of --boot N")
    sub.add_argument("--since", default="-6h", help='journalctl --since: "-30min", "-2d", "today", "2026-11-10 16:00" (default -6h)')
    sub.add_argument("--boot", type=int, nargs="?", const=0, metavar="N",
                     help="a whole boot instead of --since: this one, or N (-1 the one before; see `boots`)")
    sub.add_argument("--priority", choices=PRIORITIES, help="this level and worse")
    sub.add_argument("--grep", help="entries whose text matches this regex")
    sub.add_argument("--exclude", help="drop entries matching this regex")
    sub.add_argument("-n", "--lines", type=int, default=60, help="how many entries to show (default 60)")
    sub.add_argument("--width", type=int, default=220, help="cut longer lines (default 220)")
    unit_command("status", status)
    unit_command("cat", cat)
    commands.add_parser("failed").set_defaults(run=failed)
    commands.add_parser("timers").set_defaults(run=timers)
    commands.add_parser("boots").set_defaults(run=boots)
    sub = commands.add_parser("units")
    sub.add_argument("pattern", help="part of the unit name")
    sub.set_defaults(run=units)
    sub = commands.add_parser("ps")
    sub.add_argument("pattern", help="regex over the whole line: name, arguments, user, unit")
    sub.add_argument("--width", type=int, default=220, help="cut longer lines (default 220)")
    sub.set_defaults(run=ps)
    sub = commands.add_parser("blame")
    sub.add_argument("-n", "--lines", type=int, default=15, help="how many units (default 15)")
    sub.set_defaults(run=blame)
    arguments = parser.parse_args()
    arguments.run(arguments)


if __name__ == "__main__":
    main()
