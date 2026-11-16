#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Which lines this branch changed, so a warning that was already there is not read as ours.

`rust.py` and `cpp.py` both want the same three answers, and the same split of a compiler's warnings
into "on a line this branch touched", "elsewhere in a file this branch touched" and "everything
else". Copied into each of them, the two would have drifted exactly the way this checkout exists to
stop - see the README.

A warning is a tuple whose first two entries are an absolute path and a line number; what a caller
puts after that is its own business.
"""
import os
import re
import subprocess

HUNK = re.compile(r"\+(?P<start>\d+)(?:,(?P<count>\d+))?")


def _run(repo, command):
    # errors="replace": `git diff` carries both sides of a file, and a byte that is not UTF-8 in
    # some old blob should not turn a build check into a traceback.
    return subprocess.run(command, cwd=repo, capture_output=True, text=True, errors="replace")


def has_revision(repo, revision):
    """Whether the diff base exists. A fresh repository has no master yet, and a check that cannot
    run is not a reason to fail a build."""
    return _run(repo, ["git", "rev-parse", "--verify", "--quiet", revision]).returncode == 0


def changed_lines(repo, against):
    """Line ranges this branch touched, as {absolute path: [(first, last), ...]}.

    Both the committed diff against the base and the working tree, because a warning introduced by
    an edit that is not committed yet is still ours.
    """
    ranges = {}

    for source in (["git", "diff", "-U0", f"{against}...HEAD"], ["git", "diff", "-U0", "HEAD"]):
        current = None

        for line in _run(repo, source).stdout.splitlines():
            if line.startswith("+++ b/"):
                current = os.path.normpath(os.path.join(repo, line[6:].strip()))
            elif line.startswith("@@") and current:
                match = HUNK.search(line)

                if match:
                    start = int(match["start"])
                    ranges.setdefault(current, []).append(
                        (start, start + max(int(match["count"] or 1), 1) - 1))

    return ranges


def changed_files(repo, against):
    """Absolute paths of the files this branch touched, committed or not."""
    files = {line.strip()
             for line in _run(repo, ["git", "diff", "--name-only", f"{against}...HEAD"]).stdout.splitlines()
             if line.strip()}
    files |= {line[3:].strip()
              for line in _run(repo, ["git", "status", "--porcelain"]).stdout.splitlines()
              if line.strip()}

    return {os.path.normpath(os.path.join(repo, path)) for path in files}


def split(repo, against, warnings):
    """(introduced, nearby, touched) - or None when the diff base does not exist here.

    `introduced` sits on a line this branch changed, `nearby` was already in a file it touched.
    """
    if not has_revision(repo, against):
        return None

    touched = changed_files(repo, against)
    ranges = changed_lines(repo, against)
    introduced = sorted(warning for warning in warnings
                        if any(start <= warning[1] <= end for start, end in ranges.get(warning[0], [])))
    nearby = sorted(warning for warning in warnings
                    if warning[0] in touched and warning not in introduced)

    return introduced, nearby, touched
