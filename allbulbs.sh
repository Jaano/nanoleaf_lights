#!/usr/bin/env bash
# allbulbs.sh — run a cli.py command on every device config file sequentially.
#
# Usage:
#   ./allbulbs.sh <subcommand> [args...]
#
# Examples:
#   ./allbulbs.sh info
#   ./allbulbs.sh set --on
#   ./allbulbs.sh set --brightness 80
#   ./allbulbs.sh scene --list
#   ./allbulbs.sh scene --add "Police 1"
#   ./allbulbs.sh scene --delete 0x01
#   ./allbulbs.sh pair
#
# The script appends --conf <file> to every invocation.
# Processes all NL*.json files in the script directory, sorted alphabetically.
# Errors from individual bulbs are reported but execution continues.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="$SCRIPT_DIR/.venv/bin/python $SCRIPT_DIR/cli.py"

if [[ $# -eq 0 ]]; then
    echo "Usage: $(basename "$0") <subcommand> [args...]"
    echo ""
    echo "Examples:"
    echo "  $(basename "$0") info"
    echo "  $(basename "$0") set --on"
    echo "  $(basename "$0") scene --list"
    exit 1
fi

# Collect device config files: NL*.json sorted alphabetically.
configs=()
while IFS= read -r -d '' f; do
    configs+=("$f")
done < <(find "$SCRIPT_DIR" -maxdepth 1 -name "NL*.json" -print0 | sort -z)

if [[ ${#configs[@]} -eq 0 ]]; then
    echo "No device config files found in $SCRIPT_DIR"
    exit 1
fi

pass=0
fail=0

for conf in "${configs[@]}"; do
    name=$(python3 -c "import json; d=json.load(open('$conf')); print(d.get('name', '$conf'))" 2>/dev/null || basename "$conf")
    echo ""
    echo "══ $name  ($( basename "$conf" )) ══════════════════════════════════════"
    echo "python cli.py $* --conf $(basename "$conf")"
    if $CLI "$@" --conf "$conf"; then
        (( pass++ )) || true
    else
        echo "  [FAILED]"
        (( fail++ )) || true
    fi
done

echo ""
echo "Done: $pass ok, $fail failed  (${#configs[@]} devices)"
