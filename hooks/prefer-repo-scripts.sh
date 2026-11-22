#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

#
# PreToolUse/Bash hook: refuse hand-built command lines that a script in Scripts/ already owns.
#
# CLAUDE.md documents the scripts, and the documentation alone does not hold: a reflex like
# `gh issue list` or `rg -l pattern` gets written before the rule is consulted, and every unique
# command line asks for permission again — which is exactly what the scripts exist to avoid.
# This hook moves the rule from "remembered" to "enforced".
#
# Blocked:   gh issue list                  ->  python3 Scripts/repo.py issues
#            git commit -m "..."            ->  python3 Scripts/repo.py commit
#            git log --oneline              ->  python3 Scripts/repo.py log
#            gitleaks dir . --redact        ->  python3 Scripts/repo.py leaks
#            psql -c "select 1"             ->  python3 Scripts/db.py query
#            rg -l "SomeSymbol"             ->  python3 Scripts/find.py SomeSymbol
#            fd -e razor . Components       ->  python3 Scripts/find.py / the Glob tool
#            dotnet build / restore         ->  python3 Scripts/dotnet.py verify
#            dotnet list package            ->  python3 Scripts/dotnet.py packages
#            dotnet run script.cs           ->  python3 Scripts/dotnet.py script NAME
#            cargo build / test / clippy    ->  python3 Scripts/rust.py verify
#            cmake --build / ctest          ->  python3 Scripts/cpp.py verify
#            grep/find/sed/awk over files   ->  the Grep, Glob and Read tools
#            python3 -c / python3 <<'PY'    ->  find.py, Read, or a script written to a file
# Allowed:   git status / git diff, gh api, and every one of those tools behind a pipe,
#            where it filters another command's output instead of reading the tree.
#
# Working through the scripts and the harness tools is the rule, and it has no exception. There used
# to be a NO_SCRIPT=1 opt-out for "the one-off nothing covers"; it was removed on 2026/08/21
# because it stopped being rare - the escape hatch is easier to type than the right subcommand, so it
# became the first reflex instead of the last resort. A script that cannot do the thing is a reason
# to extend the script.
#
# Two things this hook got wrong until 2026/08/16, both of which let the rule lapse in silence:
#
#   * `\grep` slipped through every matcher. The anchor expects the command right after a separator,
#     and the backslash sits in between - so the alias-escaping habit the memory asks for (`\ls`,
#     `\cat`, because the profile aliases ls to eza) quietly switched the whole hook off.
#   * Only recursion counted. `grep -oP ... | sort -u | uniq -c` is not a recursive search but is
#     exactly the hand-built pipeline the scripts exist to replace, and find/sed/awk were not
#     mentioned at all.
#
# And a third, found on 2026/08/17: `python3` has to stay available - it is how every script here is
# run - so inline python was never matched, and became the replacement grep the moment the honest
# spellings were refused. An `os.walk` hunting for a file is find.py; printing a slice of a .cs file
# is Read. The interpreter is not the problem, code on the command line is.
#
# Reads the hook payload on stdin, writes a PreToolUse deny decision on stdout when it objects.

set -uo pipefail

payload="$(cat)"
command="$(printf '%s' "$payload" | jq -r '.tool_input.command // ""')"

deny() {
    jq -nc --arg reason "$1" \
        '{hookSpecificOutput: {hookEventName: "PreToolUse",
                               permissionDecision: "deny",
                               permissionDecisionReason: $reason}}'
    exit 0
}

# What may sit between a separator and the command name without hiding it: VAR=value prefixes, an
# `xargs` (with its own flags), and the backslash that escapes a shell alias.
prefix='([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*(xargs[[:space:]]+(-[^[:space:]]+[[:space:]]+)*)?\\?'

# Anywhere a command can start: line start, or after ; && || ( - the pipe included.
start="(^|[;&|(][[:space:]]*)${prefix}"

# The same, minus the pipe. Behind a pipe grep/sed/awk/head filter another command's output, which
# is what they are for and what no script replaces; at the start of a line they read the tree, and
# that is the Grep, Glob and Read tools' job.
reads_files="(^|[;&(][[:space:]]*)${prefix}"

matches() {
    printf '%s' "$command" | grep -qE "$1"
}

if matches "${start}gh[[:space:]]+(issue|pr|label)\b"; then
    deny "Scripts/repo.py owns the GitHub workflow: issues, issue, issue-create, comment, close, \
label, pr, pr-create, pr-edit, pr-merge. It sets GH_PAGER, reads --json instead of the localized \
human output, and carries the guards (no merge unless GitHub reports MERGEABLE). Run \
'python3 Scripts/repo.py --help' and use the subcommand. 'gh api' and 'gh repo' are not covered \
and stay available."
fi

if matches "${start}git[[:space:]]+(commit|push)\b" \
    || matches "${start}git[[:space:]]+(checkout[[:space:]]+-b|switch[[:space:]]+-c)\b"; then
    deny "Scripts/repo.py owns branching, committing and pushing: 'branch' refuses a dirty tree \
and branches off an up-to-date master, 'commit' refuses \
master without --allow-master, 'push' sets the upstream. Read-only git (status, diff, show) \
is fine as it is."
fi

# `-C` included: pointing git at the checkout next door is the same habit, one flag further along.
if matches "${start}git[[:space:]]+(-C[[:space:]]+[^[:space:]]+[[:space:]]+|--no-pager[[:space:]]+)*log\b"; then
    deny "Scripts/repo.py owns the history: 'python3 Scripts/repo.py log [PATH ...] [-n 20] \
[--range master..HEAD]' prints one line per commit with no pager to remember. For who wrote a \
passage and why, 'repo.py blame FILE --lines 24,48'."
fi

if matches "${start}gitleaks\b"; then
    deny "Scripts/repo.py owns the secret scan: 'python3 Scripts/repo.py leaks' runs gitleaks over the whole \
history and over the files a commit would take, ignored ones (build/, target/, obj/) left out, with the \
secrets redacted and one line per finding. It exits 1 on a finding."
fi

if matches "${start}psql\b"; then
    deny "Scripts/db.py owns the database connection - host, port, database and user come from \
Scripts/project.toml and its {PREFIX}_DB_* overrides, with the password in ~/.pgpass, so no \
connection string gets retyped. Use 'db.py query' \
for SQL without nested shell quoting, 'count' and 'columns' for schema.Table, 'enum-audit', or \
'apply' to run a .sql file as one transaction."
fi

if matches "${start}git[[:space:]]+grep\b"; then
    deny "'git grep' is the same raw sweep as grep, one subcommand further along, and it slipped past \
this hook until 2026/08/21 for exactly that reason. Use 'python3 Scripts/find.py Symbol' for a symbol \
and 'python3 Scripts/find.py \"some text\" --text' for anything else - the latter searches every file \
type without a word boundary, which is what git grep was being used for."
fi

# A pipe with nothing flowing through it: `true | grep -n x file` reads the file exactly like `grep -n x file`,
# with the letter of "behind a pipe" satisfied. Used a dozen times in one session on 2026/11/10 while every
# honest spelling was refused. `cmd || true` is two pipes and is not meant.
empty_source="(true|:|echo([[:space:]]+(-n[[:space:]]+)?(''|\"\"))?|printf[[:space:]]+(''|\"\"))"
if matches "(^|[;&|(][[:space:]]*)${empty_source}[[:space:]]*\|([^|]|$)"; then
    deny "A pipe fed by 'true', ':', an empty echo or printf carries nothing, so the command behind it reads its \
files directly - the thing 'behind a pipe' is not meant to allow. Text search is \
'python3 Scripts/find.py \"text\" --text' (with '--in /absolute/path' outside the repository), reading a file \
is the Read tool."
fi

if matches "${start}rg\b" || matches "${reads_files}grep\b"; then
    deny "For 'where is this symbol used, and is it used at all', use \
'python3 Scripts/find.py Symbol [--lines] [--all]' - it leaves out the directories the build keeps a \
second copy of everything in (obj/ and bin/ for a solution, target/ for a crate) and it exits 1 when \
the symbol appears nowhere. For any other text search use the Grep tool, which needs no shell \
quoting and no alias escaping. Behind a pipe grep stays available as a filter."
fi

if matches "${reads_files}find\b"; then
    deny "Locating files by name or pattern is the Glob tool's job, and 'where is this symbol used' \
is 'python3 Scripts/find.py Symbol'. A raw find re-implements both and walks obj/ and bin/, where \
the source generators emit a second copy of every file. Behind a pipe find stays available."
fi

# fd reads the tree wherever it stands, so behind a pipe too. The global instructions recommend it over
# find, which is exactly why it came back on 2026/09/25 - three times in one session, once into the
# NuGet cache, where 'find.py --in' reads just as well.
if matches "${start}fd\b"; then
    deny "In a repository with Scripts/find.py, that comes first - the global 'use fd' advice is for \
places without it. 'python3 Scripts/find.py Symbol' for code, 'python3 Scripts/find.py \"text\" --text' \
for anything else (a file is found by the class or text inside it), and '--in /absolute/path' reaches \
a neighbouring checkout or ~/.nuget just the same. Listing a directory is '\\ls DIR'."
fi

if matches "${reads_files}(sed|awk)\b"; then
    deny "Reading or slicing a file is the Read tool, which takes offset and limit and needs no \
line arithmetic; changing one is the Edit tool, which fails loudly instead of silently matching \
nothing. Behind a pipe sed and awk stay available as filters on another command's output."
fi

# wc is deliberately not here: counting lines or bytes is the one thing Read does not do.
if matches "${reads_files}(cat|head|tail)[[:space:]]+[^|]*[^[:space:]|]"; then
    deny "Reading a file - whole, first lines or last - is the Read tool. It reports the line \
numbers, handles the encoding, and does not need the alias escaping this machine's profile forces \
on cat and ls. Behind a pipe these stay available as filters."
fi

# Only inline code is refused, never the interpreter: `-c` and a heredoc have to sit where python's
# own flags sit, so `python3 Scripts/db.py query -c "SELECT 1"` - where the -c belongs to db.py - is
# untouched. Blocked behind a pipe too, unlike sed and awk: a filter can be written in the shell or
# put in a file, and "just this once, inline" is how the habit came back.
if matches "${start}python3?[[:space:]]+(-[A-Za-z]+[[:space:]]+)*(-c[[:space:]'\"]|-?[[:space:]]*<<)"; then
    deny "python3 is how every script here runs, but code on the command line is not. Searching is \
'python3 Scripts/find.py Symbol', reading a file is the Read tool (it takes offset and limit), and \
listing or locating files is Glob. When a genuine data question has no tool behind it - parsing an \
export, inspecting an archive - write the script to a file and run that: Scripts/ if it will be \
wanted again, the scratchpad if it truly will not. A file can be read back, corrected and re-run; a \
heredoc is gone once it scrolls past."
fi

if matches "${start}cargo[[:space:]]+(build|check|test|clippy|fmt)\b"; then
    deny "Scripts/rust.py owns the build: 'verify' runs clippy, the tests, rustfmt and the licence \
check, and reports the warnings on the lines this branch changed separately from the ones that were \
already there. 'warnings --code clippy::x' lists one lint, 'test --details' prints the assertion \
that failed, 'fmt' writes the formatting. See 'python3 Scripts/rust.py --help'. cargo run, add, \
fetch, tree and metadata stay available."
fi

if matches "${start}(cmake|ctest)\b"; then
    deny "Scripts/cpp.py owns the build: 'verify' configures, builds clean, runs ctest, compares \
against clang-format and checks the licence lines, and reports warnings on the lines you changed. \
'build', 'test [NAME]', 'warnings [--flag=-Wx]', 'format', 'sanitize' (the tests under ASan and UBSan), \
'fuzz' (libFuzzer), 'run -- ARGS', 'install' and 'toolchain' cover the rest, and each one reads the build directory, the generator and the install prefix out of \
Scripts/project.toml instead of having them retyped. See 'python3 Scripts/cpp.py --help'."
fi

# restore and list went through until 2026/09/25. `dotnet run` stays open for launching an app; only a
# single-file script is refused, which is what 'dotnet.py script' is for.
if matches "${start}dotnet[[:space:]]+(build|test|ef|restore|list)\b" \
    || matches "${start}dotnet[[:space:]]+run[[:space:]]+[^|;&]*\.cs\b"; then
    deny "Scripts/dotnet.py owns the build: 'verify' restores, builds, tests, reports warnings on the \
lines you changed and checks the db-init schema, all with English SDK output on a machine whose SDK is \
German. 'packages [--vulnerable|--outdated|--versions ID]' and 'projects' answer what 'dotnet list' \
would, 'script NAME' runs Scripts/csharp/NAME.cs, and 'ef' fills in the --project/--startup-project \
pair. See 'python3 Scripts/dotnet.py --help'."
fi

exit 0
