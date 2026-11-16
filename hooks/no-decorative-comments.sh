#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

#
# PreToolUse/Edit|Write|MultiEdit hook: refuse the three comment shapes that only ever grow.
#
#   1. <remarks> and <para> in an XML doc comment. Both exist to hold more prose, and that is the
#      whole problem: a <summary> is one sentence because there is nowhere else to put the second.
#      What genuinely needs a paragraph is policy and belongs in a skill under .claude/skills/, once,
#      instead of in a <remarks> on every call site.
#   2. Comments in .razor files. Markup states what it renders; a comment beside it restates it.
#      Anything that needs saying about a component belongs in its .razor.cs, where the reasoning
#      sits next to the code that has it.
#   3. Comments in .css files. Same, and worse: CSS comments in this repository were restating the
#      property below them.
#   4. Banner and separator comments, plus /* */ blocks, in a .rs or C++ file -
#      `// ===============`, `// ----- Helpers -----`, `//////////////`. They mark sections in a
#      file that should have been split, they drift out of step with what sits under them, and a
#      diff that adds one says nothing about the code. A `mod`, an `impl` or a namespace already
#      says where a section starts, and a doc comment is `///`, never `/** */`.
#
# It compares against what is being replaced, so carrying an existing block along in a larger edit
# passes and only *adding* one is refused. A Write is a whole file and is judged whole.
#
# There is no opt-out, on purpose - a NO_SCRIPT=1 variable was one for the script rule and turned into the
# first reflex rather than the last resort. Where a rule is wrong, change the rule.
#
# Reads the hook payload on stdin, writes a PreToolUse deny decision on stdout when it objects.

set -uo pipefail

payload="$(cat)"
path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // ""')"

[ -n "$path" ] || exit 0

# What this edit writes, and what it replaces. An Edit carries one pair, a MultiEdit several, a
# Write only the new side.
new_text="$(printf '%s' "$payload" | jq -r '
    [.tool_input.content // "",
     .tool_input.new_string // "",
     (.tool_input.edits // [] | map(.new_string // "") | join("\n"))] | join("\n")')"

old_text="$(printf '%s' "$payload" | jq -r '
    [.tool_input.old_string // "",
     (.tool_input.edits // [] | map(.old_string // "") | join("\n"))] | join("\n")')"

case "$path" in
    *.razor.cs|*.cs)
        # Only doc-comment lines. A report generator writing literal <para> into its own output is
        # not what this is about.
        pattern='^[[:space:]]*///.*</?(remarks|para)[[:space:]>/]'
        what="<remarks> and <para>"
        instead="Say it in the <summary> in one sentence, or drop it. A paragraph of design intent is
policy: put it in the matching skill under .claude/skills/, once."
        ;;
    *.razor)
        pattern='@\*|<!--'
        what="comments in a .razor file"
        instead="Markup states what it renders. What needs explaining belongs in the .razor.cs beside
the code that does it."
        ;;
    *.css|*.scss)
        pattern='/\*'
        what="comments in a stylesheet"
        instead="A rule that needs a comment usually needs a clearer selector or a token name."
        ;;
    *.rs|*.cpp|*.cc|*.cxx|*.h|*.hpp|*.hxx)
        # A separator: a comment line that is mostly punctuation, whatever it wraps around a word.
        pattern='^[[:space:]]*(/{4,}|//[/=*#~_-]*[[:space:]]*[=*#~_-]{4,}|/\*)'
        what="banner comments and /* */ blocks"
        instead="A section that needs a banner needs a module. Anything worth saying goes in a // line
next to the code that has the reasoning, or in a /// doc comment on the item."
        ;;
    *)
        exit 0
        ;;
esac

added="$(printf '%s' "$new_text" | grep -Ec "$pattern")"
removed="$(printf '%s' "$old_text" | grep -Ec "$pattern")"

[ "$added" -gt "$removed" ] || exit 0

reason="Refused: ${what} are not written in this repository, and this edit adds $((added - removed)).

${instead}

The rule and its reasoning are in .claude/CLAUDE.md under 'Comments and Documentation'."

jq -nc --arg reason "$reason" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse",
                           permissionDecision: "deny",
                           permissionDecisionReason: $reason}}'

exit 0
