#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

#
# Tests for no-decorative-comments.sh. Run after touching the hook:
#
#     bash .claude/hooks/no-decorative-comments.test.sh
#
# Each case feeds a payload and checks whether the hook denies.

set -uo pipefail

HOOK="$(dirname "$0")/no-decorative-comments.sh"
FAILED=0
CASE=0

write_payload() {
    jq -nc --arg path "$1" --arg content "$2" \
        '{tool_name: "Write", tool_input: {file_path: $path, content: $content}}'
}

edit_payload() {
    jq -nc --arg path "$1" --arg old "$2" --arg new "$3" \
        '{tool_name: "Edit", tool_input: {file_path: $path, old_string: $old, new_string: $new}}'
}

# check <deny|allow> <label> <payload>
check() {
    CASE=$((CASE + 1))
    local expectation="$1" label="$2" payload="$3" output

    output="$(printf '%s' "$payload" | bash "$HOOK")"

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

# --- C# doc comments ---------------------------------------------------------------------------

check deny "a <remarks> block in a .cs file" \
    "$(write_payload /repo/Thing.cs '/// <summary>Does a thing.</summary>
/// <remarks>And here is why.</remarks>
public void Thing() { }')"

check deny "a <para> inside a doc comment" \
    "$(write_payload /repo/Thing.cs '/// <summary>
///     <para>First.</para>
/// </summary>')"

check deny "a <remarks> added to a .razor.cs" \
    "$(edit_payload /repo/Thing.razor.cs '/// <summary>A.</summary>' \
        '/// <summary>A.</summary>
/// <remarks>B.</remarks>')"

check allow "a plain <summary>" \
    "$(write_payload /repo/Thing.cs '/// <summary>Does a thing.</summary>
public void Thing() { }')"

check allow "an ordinary // comment" \
    "$(write_payload /repo/Thing.cs '// The number the export bills under, not the one it is known by.
public int Number;')"

# The tags mean something else entirely outside a doc comment - a report generator writes them.
check allow "literal <para> in a string, not a doc comment" \
    "$(write_payload /repo/Report.cs 'var html = "<para>Text</para>";')"

# Carrying an existing block along in a bigger edit is not adding one.
check allow "an existing <remarks> moved through an edit unchanged" \
    "$(edit_payload /repo/Thing.cs '/// <remarks>Old.</remarks>
public void A() { }' \
        '/// <remarks>Old.</remarks>
public void B() { }')"

check allow "deleting a <remarks>" \
    "$(edit_payload /repo/Thing.cs '/// <summary>A.</summary>
/// <remarks>Gone.</remarks>' '/// <summary>A.</summary>')"

# --- Razor -------------------------------------------------------------------------------------

check deny "a razor comment" \
    "$(write_payload /repo/Thing.razor '@* Keyed on the customer. *@
<FluentLabel/>')"

check deny "an HTML comment in razor" \
    "$(write_payload /repo/Thing.razor '<!-- the header -->
<div></div>')"

check allow "plain markup" \
    "$(write_payload /repo/Thing.razor '<FluentLabel Typo="Typography.H5">@AdminResources.Name</FluentLabel>')"

# --- Stylesheets -------------------------------------------------------------------------------

check deny "a CSS comment" \
    "$(write_payload /repo/Thing.razor.css '/* the row */
.row { display: flex; }')"

check allow "plain CSS" \
    "$(write_payload /repo/Thing.razor.css '.row { display: flex; }')"

# --- Rust and C++ ------------------------------------------------------------------------------

check deny "a banner of equals signs in a .rs file" \
    "$(write_payload /repo/src/plan.rs '// ================= Helpers =================
fn helper() {}')"

check deny "a rule of slashes in a .cpp file" \
    "$(write_payload /repo/src/main.cpp '////////////////////////////
void main() {}')"

check deny "a dashed separator in a header" \
    "$(write_payload /repo/src/widget.h '// ----- Properties -----
int value;')"

check deny "a block comment in a .cpp file" \
    "$(write_payload /repo/src/main.cpp '/* Sets the thing up. */
void setup() {}')"

check deny "a Doxygen block, which is what /// is for" \
    "$(write_payload /repo/src/widget.hpp '/** Does a thing. */
void thing();')"

check allow "an ordinary // comment in C++" \
    "$(write_payload /repo/src/main.cpp '// -b:v 0 is required or a default bitrate cap overrides -cq.
const int quality = 28;')"

check allow "a /// doc comment in C++" \
    "$(write_payload /repo/src/widget.h '/// The line above the bar.
void setLabelText(const QString &text);')"

check allow "a URL, which is slashes but not a banner" \
    "$(write_payload /repo/src/main.cpp '// See https://doc.qt.io/qt-6/qprocess.html for the detach semantics.
void detach();')"

check allow "an existing banner carried through an edit unchanged" \
    "$(edit_payload /repo/src/plan.rs '// ===== Old =====
fn a() {}' '// ===== Old =====
fn b() {}')"

check allow "deleting a banner" \
    "$(edit_payload /repo/src/plan.rs '// ===== Gone =====
fn a() {}' 'fn a() {}')"

# --- Everything else -------------------------------------------------------------------------

check allow "markdown, which is prose by definition" \
    "$(write_payload /repo/README.md '<!-- a comment --> /* another */')"

check allow "a python script" \
    "$(write_payload /repo/Scripts/db.py '# what this does')"

echo
[ $FAILED -eq 0 ] && echo "all $CASE case(s) pass" || echo "failures above"
exit $FAILED
