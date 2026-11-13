#!/usr/bin/env bash
set -euo pipefail

athena_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$athena_dir"

if [[ ! -x .venv/bin/python ]]; then
    echo "Athena's virtual environment is missing. Run these commands in $athena_dir:" >&2
    echo "  python3 -m venv .venv" >&2
    echo "  .venv/bin/python -m pip install -r requirements.txt" >&2
    echo "  .venv/bin/python -m pip install -r requirements-agent.txt" >&2
    echo "  .venv/bin/python -m pip install -r voice/requirements-wake.txt" >&2
    echo "  .venv/bin/python -m pip install -e '.[agent,wake]'" >&2
    echo "Configure the three custom ONNX model paths described in voice/README.md." >&2
    exit 1
fi

export PYTHONPATH="$athena_dir/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$athena_dir/.venv/bin/python" "$athena_dir/main.py" "$@"
