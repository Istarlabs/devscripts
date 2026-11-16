# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Self-check for system.py's journalctl call: `python3 system.test.py`."""
import argparse
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location("system", pathlib.Path(__file__).with_name("system.py"))
system = importlib.util.module_from_spec(spec)
spec.loader.exec_module(system)


def command(**given):
    defaults = {"kernel": False, "unit": None, "boot": None, "since": "-6h", "priority": None, "process": None}
    return system.journal_command(argparse.Namespace(**{**defaults, **given}), given.pop("user", []))


assert command(unit="sshd") == ["journalctl", "--no-pager", "-u", "sshd", "--since", "-6h"]
# a boot replaces --since; the kernel log is per boot and takes no --since either
assert command(boot=-1, priority="err") == ["journalctl", "--no-pager", "-b", "-1", "-p", "err"]
assert command(kernel=True) == ["journalctl", "--no-pager", "-k"]
# the kernel cuts a process name to 15 characters; the log tag is not cut
assert command(process="plasma-kwin_wayland")[-3:] == ["_COMM=plasma-kwin_way", "+", "SYSLOG_IDENTIFIER=plasma-kwin_wayland"]
print("ok")
