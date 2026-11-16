#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

#
# PreToolUse/Edit|Write|MultiEdit hook: refuse the first edit in an area whose skill has not been
# read yet.
#
# Skills are listed in the prompt by name and description; their content arrives only when the Skill
# tool is called. A description read as "I probably know this" means working without the one
# document written for the task — the blood test dialog was rebuilt on 2026/08/30 straight into the
# three FluentUI traps that `blazor-ui` spells out.
#
# The mapping from file to skill lives in the project, in .claude/skill-map; this hook only applies
# it. A project without that file gets no doorbell.
#
# It deliberately covers folders only. Asking for `istarnova` on every new .cs file was tried and
# removed on 2026/08/31: a logging partial, a settings class, a component's second half - none of
# them are helpers, and the doorbell rang on all of them. A doorbell that rings for everything is
# one nobody hears. Reaching for the library first is in CLAUDE.md, where a rule with no good
# trigger belongs.
#
# So the first edit per area is refused, once per session. Reading the skill leaves a marker and
# every later edit in that area passes untouched. It is a doorbell, not a gate.
#
# Reads the hook payload on stdin, writes a PreToolUse deny decision on stdout when it objects.

set -uo pipefail

STATE_ROOT="${TMPDIR:-/tmp}/claude-skill-doorbell"

payload="$(cat)"
path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // ""')"
tool="$(printf '%s' "$payload" | jq -r '.tool_name // ""')"
session="$(printf '%s' "$payload" | jq -r '.session_id // "unknown"')"
transcript="$(printf '%s' "$payload" | jq -r '.transcript_path // ""')"

[ -n "$path" ] || exit 0

# Which skill covers which files is the one thing that differs per project, so it is read rather
# than built in: .claude/skill-map, one "<glob> <skill>" per line, first match wins, # for a comment.
# No map means no doorbell.
MAP="${CLAUDE_PROJECT_DIR:-.}/.claude/skill-map"

[ -f "$MAP" ] || exit 0

wanted=()

while read -r pattern skill; do
    case "$pattern" in ""|\#*) continue ;; esac

    # Unquoted on purpose: the pattern is a glob and has to stay one.
    # shellcheck disable=SC2254
    case "$pattern" in "~"*) continue ;; esac

    case "$path" in $pattern) wanted+=("$skill"); break ;; esac
done < "$MAP"

# A line "~<extended regex> <skill>" (no spaces in the regex) rings on what is being written rather than
# where: logging and JSON are done in any file, so a folder cannot name them. Every matching line adds
# its skill; text files are skipped, since a skill documenting LoggerMessage is not code using it.
case "$path" in
    *.md|*.json|*.txt) ;;
    *)
        written="$(printf '%s' "$payload" | jq -r '[.tool_input.new_string, .tool_input.content,
            (.tool_input.edits[]?.new_string)] | map(select(. != null)) | join("\n")')"

        while read -r pattern skill; do
            case "$pattern" in "~"*) ;; *) continue ;; esac

            if printf '%s' "$written" | grep -qE -- "${pattern#\~}"; then
                already=0

                for known in "${wanted[@]}"; do
                    [ "$known" = "$skill" ] && already=1
                done

                [ "$already" -eq 1 ] || wanted+=("$skill")
            fi
        done < "$MAP"
        ;;
esac

[ ${#wanted[@]} -gt 0 ] || exit 0

missing=()

for skill in "${wanted[@]}"; do
    marker="${STATE_ROOT}/${session}.${skill}"

    [ -f "$marker" ] && continue

    # The transcript is the record of what has already been loaded. Checking it rather than only the
    # marker means a skill read earlier in the session still counts, whatever the reason.
    if [ -n "$transcript" ] && [ -f "$transcript" ] &&
       grep -qF "\"skill\":\"${skill}\"" "$transcript" 2>/dev/null; then
        mkdir -p "$STATE_ROOT" && touch "$marker"
        continue
    fi

    mkdir -p "$STATE_ROOT" && touch "$marker"
    missing+=("$skill")
done

[ ${#missing[@]} -gt 0 ] || exit 0

names="$(printf '%s, ' "${missing[@]}")"
names="${names%, }"
file="$(basename "$path")"

reason="First edit to ${file} this session and the skill written for it has not been read: ${names}.
Call Skill(skill: \"${missing[0]}\") — and the others if there is more than one — then repeat this
edit, which will go through. If the skill turns out not to cover this file, say so and carry on."

jq -nc --arg reason "$reason" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse",
                           permissionDecision: "deny",
                           permissionDecisionReason: $reason}}'

exit 0
