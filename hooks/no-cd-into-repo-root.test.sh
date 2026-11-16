#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

#
# Checks no-cd-into-repo-root.sh against the spellings of "cd to the root" it must refuse and the cd it must allow.
#
# The ~ and $HOME lines were run in oversteer-qt on 2026/11/10 while the hook was in place and silent.
#
#   bash hooks/no-cd-into-repo-root.test.sh

set -uo pipefail

hook="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/no-cd-into-repo-root.sh"
failures=0

home="$(mktemp -d)"
repo="$home/dev/repo"
mkdir -p "$repo/Data"
trap 'rm -rf "$home"' EXIT

check() {
    local want="$1" cmd="$2" cwd="${3:-$repo}"
    local out got
    out="$(jq -nc --arg cmd "$cmd" --arg cwd "$cwd" '{tool_input:{command:$cmd}, cwd:$cwd}' \
        | HOME="$home" CLAUDE_PROJECT_DIR="$repo" bash "$hook")"
    [ -z "$out" ] && got="ALLOW" || got="DENY"

    if [ "$got" = "$want" ]; then
        printf '  ok   %-6s %s\n' "$got" "$cmd"
    else
        printf '  FAIL want=%s got=%s  %s\n' "$want" "$got" "$cmd"
        failures=$((failures + 1))
    fi
}

echo "must be refused:"
check DENY "cd $repo && git status"
check DENY 'cd ~/dev/repo && python3 Scripts/repo.py status'
check DENY 'cd $HOME/dev/repo && python3 Scripts/repo.py status'
check DENY 'cd "${HOME}/dev/repo"; \ls'
check DENY '\ls; cd ~/dev/repo/ && \ls'
check DENY "git -C $repo status"
check DENY 'git -C ~/dev/repo status'
check DENY "R=$repo; cd \$R"

echo
echo "must keep working:"
check ALLOW 'cd Data && \ls'
check ALLOW 'cd ~/dev/other && \ls'
check ALLOW 'cd ~/dev/repo/Data && \ls'
check ALLOW 'python3 Scripts/repo.py status'
# Left in a subdirectory by an earlier call: going back to the root is the fix then.
check ALLOW 'cd ~/dev/repo && \ls' "$repo/Data"

echo
if [ "$failures" -eq 0 ]; then
    echo "all checks passed"
else
    echo "$failures check(s) failed"
    exit 1
fi
