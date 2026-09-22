#!/bin/bash
#
# Audits a Trivy ignorefile's "Review by YYYY-MM-DD" markers.
#
# Two separate failure modes, because only checking one of them lets the other
# rot forever:
#
#   1. EXPIRED  -- an entry's review-by date has passed. The suppression was a
#                  bet that upstream would ship a fix; the bet is due.
#   2. UNDATED  -- an entry has no governing review-by date at all. This is the
#                  worse of the two: the weekly re-triage literally cannot see
#                  it, so it is a permanent exception wearing a temporary
#                  exception's clothes. The previous version of this script
#                  only grepped for dates that existed and treated "zero dates
#                  found" as success, which is exactly how 46 of this file's 60
#                  entries stayed invisible (audit-2026-09-14).
#
# Attribution model: a run of consecutive comment lines is a BLOCK, and that
# block governs every suppression entry between it and the next comment block.
# An entry is dated iff its governing block contains a "Review by YYYY-MM-DD"
# marker. The phrase may wrap across comment lines, so each block is joined
# into one blob before matching.
#
# Every run prints the denominators -- entries examined, dated, undated,
# expired. Zero entries examined is a FAILURE, not a pass: a scanner pointed at
# a moved or empty path must never report clean.
#
# Used by .github/workflows/trivy-weekly-retriage.yml; also runnable locally
# before adding a new suppression entry.
#
# Exit codes:
#   0 = every entry carries a governing review-by date and none has passed.
#   1 = usage/file error, or the file contains zero suppression entries.
#   2 = one or more entries are past their review-by date.
#   3 = one or more entries have no governing review-by date.
# (2 and 3 can both apply; 3 is reported and takes the exit code.)

set -euo pipefail

IGNOREFILE="${1:-images/ollama/.trivyignore}"

if [ ! -f "$IGNOREFILE" ]; then
    echo "::error::$IGNOREFILE not found" >&2
    exit 1
fi

TODAY="$(date -u +%Y-%m-%d)"

ENTRIES=0
DATED=0
UNDATED=0
EXPIRED=0

# Current comment block, joined into one blob, and the date extracted from it.
# bash 3.2: no associative arrays, no mapfile -- plain string accumulation and
# a `while read` over the file.
block=""
block_date=""
in_comment=0

# Extract the LAST review-by date from a blob (a block that has been re-triaged
# in place may mention an older date in its prose; the operative one is last).
# awk, not `grep -o | tail -1`: under `set -e` + `pipefail` a grep that finds
# no date exits 1, and the command substitution that captures it kills the
# whole script before it can report the undated entry -- silently turning the
# "no date" case into an early exit instead of a finding. awk exits 0 whether
# or not it matches, so "no date" stays an empty string and reaches the check.
_block_date() {
    printf '%s\n' "$1" | awk '{
        last = ""
        rest = $0
        while (match(rest, /Review by [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/)) {
            last = substr(rest, RSTART + 10, 10)
            rest = substr(rest, RSTART + RLENGTH)
        }
        print last
    }'
}

while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        \#*)
            # A comment line after entries starts a new block.
            if [ "$in_comment" -eq 0 ]; then
                block=""
            fi
            in_comment=1
            stripped="$(printf '%s' "$line" | sed 's/^#[[:space:]]*//')"
            block="$block $stripped"
            continue
            ;;
    esac

    if [ "$in_comment" -eq 1 ]; then
        in_comment=0
        block_date="$(_block_date "$block")"
    fi

    # Suppression entry: a bare vulnerability/misconfig id on its own line.
    # Covers CVE-*, GHSA-*, and Trivy's AVD/KSV/DS misconfig ids.
    if printf '%s' "$line" \
        | grep -qE '^[[:space:]]*(CVE-[0-9]{4}-[0-9]+|GHSA-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}|(AVD|KSV|DS|KCV)-[0-9A-Za-z]+-[0-9]+)[[:space:]]*$'; then
        entry="$(printf '%s' "$line" | tr -d '[:space:]')"
        ENTRIES=$((ENTRIES + 1))

        if [ -z "$block_date" ]; then
            echo "UNDATED $entry (no 'Review by <date>' in its governing comment block)"
            UNDATED=$((UNDATED + 1))
        else
            DATED=$((DATED + 1))
            if [[ "$block_date" < "$TODAY" ]]; then
                echo "EXPIRED $entry review-by $block_date (today: $TODAY)"
                EXPIRED=$((EXPIRED + 1))
            else
                echo "ok      $entry review-by $block_date (today: $TODAY)"
            fi
        fi
    fi
done < "$IGNOREFILE"

echo "ignorefile=$IGNOREFILE"
echo "entries-examined=$ENTRIES"
echo "entries-dated=$DATED"
echo "entries-undated=$UNDATED"
echo "entries-expired=$EXPIRED"

# A gate that examined nothing proves nothing. Assert a non-zero denominator.
if [ "$ENTRIES" -eq 0 ]; then
    echo "::error::$IGNOREFILE contains zero suppression entries -- refusing to report clean. Either the path is wrong or the entry pattern no longer matches this file." >&2
    exit 1
fi

if [ "$UNDATED" -gt 0 ]; then
    echo "::error::$UNDATED of $ENTRIES suppression entries have no governing 'Review by <date>' marker. An undated suppression is a permanent exception the weekly re-triage cannot see." >&2
    exit 3
fi

if [ "$EXPIRED" -gt 0 ]; then
    echo "::error::$EXPIRED of $ENTRIES suppression entries are past their review-by date." >&2
    exit 2
fi

exit 0
