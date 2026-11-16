#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

#
# Tests for load-the-skill.sh. Run after touching the hook:
#
#     bash .claude/hooks/load-the-skill.test.sh
#
# Each case feeds a payload and checks whether the hook denies. The session id differs per case, so
# the once-per-session marker of one case cannot silence the next.

set -uo pipefail

HOOK="$(dirname "$0")/load-the-skill.sh"

# The hook reads $CLAUDE_PROJECT_DIR/.claude/skill-map: the shared templates plus the json lines a
# project adds itself, so the test runs from anywhere.
PROJECT="$(mktemp -d)"
trap 'rm -rf "$PROJECT"' EXIT
mkdir -p "$PROJECT/.claude"
TEMPLATES="$(dirname "$0")/../templates"
{
    cat "$TEMPLATES/skill-map.istarnova" "$TEMPLATES/skill-map.localization"
    printf '%s\n' '*/Models/Json/* json' '~JsonSerializer|JsonHelper|JsonTypeInfo json'
} > "$PROJECT/.claude/skill-map"
export CLAUDE_PROJECT_DIR="$PROJECT"
FAILED=0
CASE=0

payload() {
    jq -nc --arg tool "$1" --arg path "$2" --arg session "$3" --arg transcript "${4-}" \
        '{tool_name: $tool, tool_input: {file_path: $path}, session_id: $session,
          transcript_path: $transcript}'
}

# check <expectation: deny|allow> <expected skill or ""> <tool> <path> [transcript]
check() {
    CASE=$((CASE + 1))
    local expectation="$1" skill="$2" tool="$3" path="$4" transcript="${5-}"
    local output

    output="$(payload "$tool" "$path" "test-${CASE}-$$" "$transcript" | bash "$HOOK")"

    case "$expectation" in
        deny)
            if ! printf '%s' "$output" | grep -q '"permissionDecision":"deny"'; then
                echo "FAIL [$CASE] expected a denial for $path"
                FAILED=1
                return
            fi

            if [ -n "$skill" ] && ! printf '%s' "$output" | grep -q "$skill"; then
                echo "FAIL [$CASE] denial for $path does not name '$skill'"
                FAILED=1
                return
            fi
            ;;
        allow)
            if [ -n "$output" ]; then
                echo "FAIL [$CASE] expected no objection for $path, got: $output"
                FAILED=1
                return
            fi
            ;;
    esac

    echo "ok   [$CASE] $expectation $path"
}

check deny  blazor-ui    Edit  /repo/MyApp/Components/Pages/Thing.razor
check deny  blazor-ui    Edit  /repo/MyApp/Components/Pages/Thing.razor.cs
check deny  blazor-ui    Edit  /repo/MyApp/Components/Pages/Thing.razor.css
check deny  data-layer   Edit  /repo/MyApp.Database.Entities/Orders/Thing.cs
check deny  data-layer   Edit  /repo/MyApp.Core.Services.Repositories/Thing.cs
check deny  localization Edit  /repo/MyApp.Resources/OrderResources.de.resx
check deny  localization Edit  /repo/Scripts/translations/OrderResources.json

check deny  blazor-ui    Write /repo/MyApp/Components/Pages/New.razor.cs

# A new .cs file outside the folders above rings nothing. It used to ask for istarnova and rang on
# everything, which is why that came out again.
check allow ""           Write /repo/MyApp/Services/NewHelper.cs

# What is written can ring too, in a file no folder rule covers ("~regex skill" lines of the map).
written_check() {
    CASE=$((CASE + 1))
    local expectation="$1" skill="$2" path="$3" content="$4" output

    output="$(jq -nc --arg path "$path" --arg content "$content" --arg session "test-${CASE}-$$" \
        '{tool_name: "Write", tool_input: {file_path: $path, content: $content}, session_id: $session}' | bash "$HOOK")"

    if [ "$expectation" = deny ] && ! printf '%s' "$output" | grep -q "$skill"; then
        echo "FAIL [$CASE] expected a denial naming '$skill' for $path, got: $output"
        FAILED=1
    elif [ "$expectation" = allow ] && [ -n "$output" ]; then
        echo "FAIL [$CASE] expected no objection for $path, got: $output"
        FAILED=1
    else
        echo "ok   [$CASE] $expectation $path (by content)"
    fi
}

written_check deny  logging /repo/MyApp/Services/Thing.cs '[LoggerMessage(EventId = 1)]'
written_check deny  json    /repo/MyApp/Services/Thing.cs 'var x = JsonSerializer.Serialize(y);'
written_check allow ""      /repo/MyApp/Services/Thing.cs 'var x = 1;'
written_check allow ""      /repo/.claude/skills/logging/SKILL.md '[LoggerMessage(EventId = 1)]'
check deny  logging      Edit  /repo/MyApp/Services/Thing.Log.cs
check deny  json         Edit  /repo/MyApp.Importer/Models/Json/JsonThingContext.cs

# Nothing this repository has a skill for.
check allow ""           Edit  /repo/README.md
check allow ""           Edit  /repo/Scripts/db.py
check allow ""           Write /repo/deploy/compose.yaml

# A second edit in the same session is silent: the marker was left by the first.
SESSION="repeat-$$"
payload Edit /repo/MyApp/Components/Pages/A.razor "$SESSION" "" | bash "$HOOK" >/dev/null
SECOND="$(payload Edit /repo/MyApp/Components/Pages/B.razor "$SESSION" "" | bash "$HOOK")"
CASE=$((CASE + 1))

if [ -n "$SECOND" ]; then
    echo "FAIL [$CASE] the second edit of a session should pass, got: $SECOND"
    FAILED=1
else
    echo "ok   [$CASE] second edit in the same session passes"
fi

# A skill already loaded in the transcript counts, even on the first edit.
TRANSCRIPT="$(mktemp)"
printf '%s\n' '{"type":"tool_use","name":"Skill","input":{"skill":"blazor-ui"}}' > "$TRANSCRIPT"
LOADED="$(payload Edit /repo/MyApp/Components/Pages/C.razor "loaded-$$" "$TRANSCRIPT" | bash "$HOOK")"
CASE=$((CASE + 1))

if [ -n "$LOADED" ]; then
    echo "FAIL [$CASE] a skill named in the transcript should count, got: $LOADED"
    FAILED=1
else
    echo "ok   [$CASE] a skill already read in the transcript passes"
fi

rm -f "$TRANSCRIPT"

echo
[ $FAILED -eq 0 ] && echo "all $CASE case(s) pass" || echo "failures above"
exit $FAILED
