#!/usr/bin/env bash
# PostToolUse: run flutter analyzer on the Dart file that was just edited.
# Silently no-ops when Flutter isn't available or the path isn't Dart.

set +e

path="${1:-}"

if [[ -z "$path" || "$path" != *.dart ]]; then
  exit 0
fi

# Resolve the repo root from the working directory that contains mobile/.
while [[ ! -d "$PWD/mobile" && "$PWD" != "/" ]]; do
  cd ..
done

if [[ ! -d "$PWD/mobile" ]]; then
  exit 0
fi

cd "$PWD/mobile" || exit 0
flutter analyze "$path" 2>&1 | tail -12

exit 0