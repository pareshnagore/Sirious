#!/usr/bin/env bash
# PreToolUse guard: block edits/writes to secrets and generated files.
# Exit 0 = allow; exit 2 = block (Claude Code treats non-zero 2 as a hard block).

path="${1:-}"

# Never block when no path is provided (e.g. Write to a new unnamed target).
if [[ -z "$path" ]]; then
  exit 0
fi

# Patterns that must never be hand-edited or overwritten.
if   [[ "$path" =~ (^|/)\.env(\.[^/]+)?$ ]] \
  || [[ "$path" =~ \.(gradle|lock|wav|mp3|png|jpe?g|svg|jar|bat)$ ]] \
  || [[ "$path" =~ /(build|dist|\.dart_tool|\.idea|android/app/src/main/res)/ ]]; then
  echo "BLOCKED: '$path' is a protected file (secrets / generated / media). Refusing to edit."
  exit 2
fi

exit 0