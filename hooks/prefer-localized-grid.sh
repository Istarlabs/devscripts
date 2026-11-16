#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

#
# PreToolUse/Edit|Write|MultiEdit hook: refuse a raw <FluentDataGrid> in a .razor file of a project that uses the
# Istarnova building blocks.
#
#     <FluentDataGrid TGridItem="Row" ItemsProvider="Load"> ... </FluentDataGrid>
#
# The raw grid draws its sort and resize menus, its empty message and its paginator in English, whatever language the
# session speaks - it is how one application shipped an English grid in a German and Japanese application. The
# wrapper LocalizedDataGrid (Istarnova.Blazor.FluentUI.Grids) takes the same columns, brings its own paginator and reads the
# texts from the registered IFluentLocalizer.
#
# It only applies to a project that says it uses Istarnova: an [istarnova] block in Scripts/project.toml. Anywhere else
# a FluentDataGrid is what it is.
#
# It compares against what is being replaced, so moving an existing grid through a larger edit passes and only *adding* one
# is refused. A Write is a whole file and is judged whole.
#
# There is no opt-out, on purpose. Where the rule is wrong, change the rule.
#
# Reads the hook payload on stdin, writes a PreToolUse deny decision on stdout when it objects.

set -uo pipefail

payload="$(cat)"
path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // ""')"

case "$path" in
    *.razor) ;;
    *) exit 0 ;;
esac

config="${CLAUDE_PROJECT_DIR:-.}/Scripts/project.toml"

[ -f "$config" ] && grep -qE '^\[istarnova\]' "$config" || exit 0

new_text="$(printf '%s' "$payload" | jq -r '
    [.tool_input.content // "",
     .tool_input.new_string // "",
     (.tool_input.edits // [] | map(.new_string // "") | join("\n"))] | join("\n")')"

old_text="$(printf '%s' "$payload" | jq -r '
    [.tool_input.old_string // "",
     (.tool_input.edits // [] | map(.old_string // "") | join("\n"))] | join("\n")')"

# The opening tag only: LocalizedDataGrid and FluentDataGridRow are different words.
pattern='<FluentDataGrid([[:space:]>/]|$)'

added="$(printf '%s' "$new_text" | grep -Ec "$pattern")"
removed="$(printf '%s' "$old_text" | grep -Ec "$pattern")"

[ "$added" -gt "$removed" ] || exit 0

reason="Refused: this edit adds a raw <FluentDataGrid>.

A raw grid draws its sort and resize menus, its empty message and its paginator in English, whatever language the
session speaks. Use the wrapper from Istarnova.Blazor.FluentUI:

    <LocalizedDataGrid TGridItem=\"Row\" ItemsProvider=\"@Load\" Pagination=\"@_pagination\">
        <PropertyColumn Property=\"@(row => row.Name)\" Title=\"@Resources.Name\"/>
    </LocalizedDataGrid>

It has the same columns and parameters, a paginator of its own (delete a <FluentPaginator> under it) and reads its texts from the
IFluentLocalizer that AddIstarnovaFluentUI() registers. A list a repository pages and searches is LocalizedSearchableDataGrid.

See the blazor-ui skill, references/building-blocks.md."

jq -nc --arg reason "$reason" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse",
                           permissionDecision: "deny",
                           permissionDecisionReason: $reason}}'

exit 0
