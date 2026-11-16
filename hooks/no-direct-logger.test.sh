#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

#
# Tests for no-direct-logger.sh. Run after touching the hook:
#
#     bash .claude/hooks/no-direct-logger.test.sh
#
# Each case feeds a payload and checks whether the hook denies.

set -uo pipefail

HOOK="$(dirname "$0")/no-direct-logger.sh"
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

# --- the names a logger goes by here -----------------------------------------------------------

check deny "_logger in a repository" \
    "$(write_payload /repo/BloodTestRepository.cs \
        '_logger.LogWarning("BloodTest with ID {BloodTestId} not found", id);')"

check deny "Logger in a component code-behind" \
    "$(write_payload /repo/InvoicesTab.razor.cs \
        'Logger.LogError(ex, "Error loading invoices for CustomerId {CustomerId}", customerId);')"

check deny "a logger held under another name" \
    "$(write_payload /repo/Endpoints.cs 'downloadLogger.LogInformation("Asked for their data.");')"

check deny "a nullable logger" \
    "$(write_payload /repo/Thing.cs 'logger?.LogDebug("x");')"

check deny "a call inside a razor @code block" \
    "$(write_payload /repo/Login.razor '@code { Logger.LogInformation("User logged in."); }')"

check deny "one added by an edit" \
    "$(edit_payload /repo/Thing.cs 'return true;' \
        'Logger.LogWarning("nothing found");
return true;')"

check deny "a space before the parenthesis" \
    "$(write_payload /repo/Thing.cs 'Logger.LogError ("x");')"

# --- what the rule asks for instead --------------------------------------------------------------

check allow "a RepositoryLogger extension" \
    "$(write_payload /repo/BloodTestRepository.cs \
        '_logger.LogRepositoryNotFound(nameof(BloodTest), id);')"

check allow "a ComponentLogger extension" \
    "$(write_payload /repo/InvoicesTab.razor.cs \
        'Logger.LogComponentLoadFailed(ex, nameof(Invoice), "CustomerId", customerId);')"

check allow "a generated partial being called" \
    "$(write_payload /repo/ImportHistory.razor.cs 'LogRollbackStarted(Logger, importSessionId);')"

# The declarations themselves carry the level names and must not trip over their own attribute.
check allow "the .Log.cs that declares them" \
    "$(write_payload /repo/ImportHistory.razor.Log.cs \
        '[LoggerMessage(EventId = 6220, Level = LogLevel.Information, Message = "Rolling back {id}")]
private static partial void LogRollbackStarted(ILogger logger, long id);')"

# Carrying an existing call through a bigger edit is not adding one.
check allow "an existing call moved through an edit unchanged" \
    "$(edit_payload /repo/Thing.cs 'Logger.LogError(ex, "x");
public void A() { }' \
        'Logger.LogError(ex, "x");
public void B() { }')"

check allow "deleting one" \
    "$(edit_payload /repo/Thing.cs 'Logger.LogError(ex, "x");
return false;' 'return false;')"

# --- everything else -----------------------------------------------------------------------------

check allow "a python script that mentions one" \
    "$(write_payload /repo/Scripts/find.py 'PATTERN = r"_logger\.LogError\("')"

check allow "markdown quoting the bad shape" \
    "$(write_payload /repo/CONTRIBUTING.md 'Never write `_logger.LogError("...")`.')"

echo
[ $FAILED -eq 0 ] && echo "all $CASE case(s) pass" || echo "failures above"
exit $FAILED
