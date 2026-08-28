#!/bin/bash
#
# Scans a Trivy ignorefile for "Review by YYYY-MM-DD" markers in the leading
# comment blocks and reports any whose date has passed. This is the early
# warning that a suppression entry (calibrated on the date it was added) has
# gone stale -- the release build should not be the first place a reviewer
# notices.
#
# The phrase can wrap across a comment-line boundary (prose wraps mid-file,
# so "Review by" ends one `#` line and the date starts the next), so this
# joins every leading comment line into one blob before searching rather than
# grepping line-by-line.
#
# Used by .github/workflows/trivy-weekly-retriage.yml; also runnable locally
# before adding a new suppression entry.
#
# Exit codes: 0 = no expired entries (including "no dates found" -- an
# ignorefile with zero review-by markers isn't a failure of this script).
# 1 = usage/file error. 2 = one or more entries are past their review-by date.

set -euo pipefail

IGNOREFILE="${1:-images/ollama/.trivyignore}"

if [ ! -f "$IGNOREFILE" ]; then
    echo "::error::$IGNOREFILE not found" >&2
    exit 1
fi

TODAY="$(date -u +%Y-%m-%d)"

# Strip the leading '#' + optional space from every comment line, join with
# spaces so a "Review by" / date split across two comment lines still reads
# as one phrase.
BLOB="$(grep '^#' "$IGNOREFILE" | sed 's/^#[[:space:]]*//' | tr '\n' ' ')"

DATES="$(echo "$BLOB" | grep -oE 'Review by [0-9]{4}-[0-9]{2}-[0-9]{2}' | awk '{print $3}' || true)"

FOUND=0
EXPIRED=0

if [ -n "$DATES" ]; then
    while IFS= read -r d; do
        [ -z "$d" ] && continue
        FOUND=$((FOUND + 1))
        if [[ "$d" < "$TODAY" ]]; then
            echo "EXPIRED review-by $d (today: $TODAY)"
            EXPIRED=$((EXPIRED + 1))
        else
            echo "ok review-by $d (today: $TODAY)"
        fi
    done <<< "$DATES"
fi

echo "review-by-dates-found=$FOUND"
echo "review-by-dates-expired=$EXPIRED"

if [ "$EXPIRED" -gt 0 ]; then
    exit 2
fi

exit 0
