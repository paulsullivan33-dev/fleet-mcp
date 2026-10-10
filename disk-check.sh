#!/bin/bash
# disk-check.sh — report disk space on a fleet host over passwordless SSH.
# Usage: ./disk-check.sh [user@]host
#
# Prints `df -h` for real filesystems (skips tmpfs/devtmpfs/overlay noise),
# plus a one-line summary naming the fullest filesystem.
set -u

host="${1:?usage: disk-check.sh [user@]host}"

report=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" \
    'df -h -x tmpfs -x devtmpfs -x overlay 2>/dev/null || df -h') \
    || { echo "ERROR: ssh to $host failed"; exit 1; }

printf '%s\n' "$report"
printf '\n'
printf '%s\n' "$report" | awk '
    NR == 1 { next }
    NF >= 6 {
        pct = $5; gsub(/%/, "", pct)
        if (pct + 0 > max) { max = pct + 0; fs = $1 " mounted on " $6 }
    }
    END { if (max != "") printf "fullest: %s at %s%%\n", fs, max }
'
