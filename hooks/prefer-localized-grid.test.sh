#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

#
# Tests for prefer-localized-grid.sh. Run after touching the hook:
#
#     bash ~/dev/tools/devscripts/hooks/prefer-localized-grid.test.sh
#
# Each case feeds a payload and checks whether the hook denies. The hook only applies to a project with an
# [istarnova] block, so there is one project that has it and one that does not.

set -uo pipefail

HOOK="$(dirname "$0")/prefer-localized-grid.sh"
FAILED=0
CASE=0

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/with/Scripts" "$WORK/without/Scripts"
printf 'name = "x"\n\n[istarnova]\nraw_grid_baseline = 0\n' > "$WORK/with/Scripts/project.toml"
printf 'name = "y"\n\n[dotnet]\nsolution = "Y.sln"\n' > "$WORK/without/Scripts/project.toml"

write_payload() {
    jq -nc --arg path "$1" --arg content "$2" \
        '{tool_name: "Write", tool_input: {file_path: $path, content: $content}}'
}

edit_payload() {
    jq -nc --arg path "$1" --arg old "$2" --arg new "$3" \
        '{tool_name: "Edit", tool_input: {file_path: $path, old_string: $old, new_string: $new}}'
}

# check <deny|allow> <project> <label> <payload>
check() {
    CASE=$((CASE + 1))
    local expectation="$1" project="$2" label="$3" payload="$4" output

    output="$(printf '%s' "$payload" | CLAUDE_PROJECT_DIR="$WORK/$project" bash "$HOOK")"

    case "$expectation" in
        deny)
            if ! printf '%s' "$output" | grep -q '"permissionDecision":"deny"'; then
                echo "FAIL [$CASE] expected a denial: $label"
                FAILED=1
                return
            fi
            ;;
        allow)
            if [ -n "$output" ]; then
                echo "FAIL [$CASE] expected no objection: $label - got: $output"
                FAILED=1
                return
            fi
            ;;
    esac

    echo "ok   [$CASE] $expectation $label"
}

# --- a raw grid, in a project that uses Istarnova -------------------------------------------------

check deny with "a raw grid in a new page" \
    "$(write_payload /repo/Pages/List.razor '<FluentDataGrid TGridItem="Row" Items="@_rows"><PropertyColumn Property="@(r => r.Name)"/></FluentDataGrid>')"

check deny with "a raw grid on its own line" \
    "$(write_payload /repo/Pages/List.razor '<FluentStack>
    <FluentDataGrid
        TGridItem="Row">
    </FluentDataGrid>
</FluentStack>')"

check deny with "a grid without attributes" \
    "$(write_payload /repo/Pages/List.razor '<FluentDataGrid>x</FluentDataGrid>')"

check deny with "one added by an edit" \
    "$(edit_payload /repo/Pages/List.razor '<p>old</p>' '<FluentDataGrid TGridItem="Row" Items="@_rows"></FluentDataGrid>')"

# --- what the rule asks for instead ---------------------------------------------------------------

check allow with "the localized grid" \
    "$(write_payload /repo/Pages/List.razor '<LocalizedDataGrid TGridItem="Row" Items="@_rows"></LocalizedDataGrid>')"

check allow with "a row of a grid is a different word" \
    "$(write_payload /repo/Pages/List.razor '<FluentDataGridRow>x</FluentDataGridRow>')"

check allow with "an existing grid moved through a bigger edit" \
    "$(edit_payload /repo/Pages/List.razor '<FluentDataGrid TGridItem="Row">
<p>a</p>' '<FluentDataGrid TGridItem="Row">
<p>b</p>')"

check allow with "replacing the raw grid by the localized one" \
    "$(edit_payload /repo/Pages/List.razor '<FluentDataGrid TGridItem="Row"></FluentDataGrid>' '<LocalizedDataGrid TGridItem="Row"></LocalizedDataGrid>')"

# --- everything else ------------------------------------------------------------------------------

check allow with "a code-behind that names the type" \
    "$(write_payload /repo/Pages/List.razor.cs 'private FluentDataGrid<Row>? _grid; // <FluentDataGrid
')"

check allow with "markdown quoting the tag" \
    "$(write_payload /repo/README.md 'Never write <FluentDataGrid TGridItem="Row">.')"

check allow without "a raw grid in a project that does not use Istarnova" \
    "$(write_payload /repo/Pages/List.razor '<FluentDataGrid TGridItem="Row" Items="@_rows"></FluentDataGrid>')"

echo
[ $FAILED -eq 0 ] && echo "all $CASE case(s) pass" || echo "failures above"
exit $FAILED
