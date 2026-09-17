#!/usr/bin/env bash
set -euo pipefail

athena_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$athena_dir"

if [[ ! -x .venv/bin/python ]]; then
    echo "Athena's virtual environment is missing. Run these commands in $athena_dir:" >&2
    echo "  python3 -m venv .venv" >&2
    echo "  .venv/bin/python -m pip install -r requirements.txt" >&2
    exit 1
fi

exec "$athena_dir/.venv/bin/python" "$athena_dir/main.py" "$@"
