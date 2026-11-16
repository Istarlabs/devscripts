#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

#
# PreToolUse/Edit|Write|MultiEdit hook: refuse a direct ILogger call in C#.
#
#     _logger.LogWarning("Customer with ID {CustomerId} not found", id);
#
# Two things are wrong with that line and neither is style. It formats the message at runtime whether
# or not the level is on, and - the part that actually bit - a sentence written at the call site is
# where a person's name, a document's file name or a search term ends up in a log table that may be
# exported and handed to a stranger.
#
# What to write instead, in this order:
#
#   1. A repository -> RepositoryLogger: LogRepositoryNotFound, LogRepositoryException, ...
#   2. A page, tab or dialog -> ComponentLogger: LogComponentLoadFailed, LogComponentSaveFailed, ...
#   3. Anything else, or a message nobody else says -> a {ClassName}.Log.cs partial with
#      [LoggerMessage]. Components keep the .razor. prefix and need a DependentUpon entry.
#
# A message repeated across classes belongs in one of the shared loggers, not in a tenth near-copy of
# a partial - that is the rule #654 settled.
#
# It compares against what is being replaced, so moving an existing call through a larger edit passes
# and only *adding* one is refused. A Write is a whole file and is judged whole.
#
# There is no opt-out, on purpose. Where the rule is wrong, change the rule.
#
# Reads the hook payload on stdin, writes a PreToolUse deny decision on stdout when it objects.

set -uo pipefail

payload="$(cat)"
path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // ""')"

[ -n "$path" ] || exit 0

case "$path" in
    # The generated methods live in these, declared rather than called.
    *.Log.cs) exit 0 ;;
    *.cs|*.razor) ;;
    *) exit 0 ;;
esac

new_text="$(printf '%s' "$payload" | jq -r '
    [.tool_input.content // "",
     .tool_input.new_string // "",
     (.tool_input.edits // [] | map(.new_string // "") | join("\n"))] | join("\n")')"

old_text="$(printf '%s' "$payload" | jq -r '
    [.tool_input.old_string // "",
     (.tool_input.edits // [] | map(.old_string // "") | join("\n"))] | join("\n")')"

# A logger by any of the names it goes by here: _logger, Logger, logger, downloadLogger. Not
# LoggerMessage, and not a Log* method of our own - those are the answer, not the problem.
pattern='[A-Za-z_]*[Ll]ogger\??\.Log(Information|Warning|Error|Debug|Critical|Trace)[[:space:]]*\('

added="$(printf '%s' "$new_text" | grep -Ec "$pattern")"
removed="$(printf '%s' "$old_text" | grep -Ec "$pattern")"

[ "$added" -gt "$removed" ] || exit 0

reason="Refused: this edit adds $((added - removed)) direct ILogger call(s).

Logging here goes through a generated [LoggerMessage] method, in this order:

  1. A repository        -> RepositoryLogger  (LogRepositoryNotFound, LogRepositoryException, ...)
  2. A page/tab/dialog   -> ComponentLogger   (LogComponentLoadFailed, LogComponentSaveFailed, ...)
  3. Anything else       -> a {ClassName}.Log.cs partial with [LoggerMessage]

A message other classes also say belongs in the shared logger, not in a tenth copy of a partial.

And read the message before writing it: no personal data - names, birth dates, identifiers, anything
tied to a person, file names or search terms. Ids are what belong there.

The rule and its reasoning are in .claude/CLAUDE.md under 'Logging Guidelines'."

jq -nc --arg reason "$reason" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse",
                           permissionDecision: "deny",
                           permissionDecisionReason: $reason}}'

exit 0
