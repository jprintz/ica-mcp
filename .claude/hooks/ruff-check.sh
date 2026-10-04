#!/usr/bin/env bash
# PostToolUse hook (Edit|Write): lint a Python file right after an agent edits it.
# Exit 2 sends ruff's output back to the model so it fixes the file; anything
# else is silent. Reads the hook payload from stdin.
set -u
f=$(python3 -c 'import json,sys
d=json.load(sys.stdin)
print(d.get("tool_input",{}).get("file_path") or d.get("tool_response",{}).get("filePath") or "")' 2>/dev/null) || exit 0
case "$f" in *.py) ;; *) exit 0 ;; esac
[ -f "$f" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
if [ -x .venv/bin/ruff ]; then RUFF=.venv/bin/ruff
elif command -v ruff >/dev/null 2>&1; then RUFF=ruff
elif command -v uvx >/dev/null 2>&1; then RUFF="uvx ruff"
else exit 0; fi
out=$($RUFF check --quiet --output-format concise "$f" 2>&1) && exit 0
printf 'ruff check %s:\n%s\nFix these before moving on (ruff check --fix handles the [*] ones).\n' "$f" "$out" >&2
exit 2
