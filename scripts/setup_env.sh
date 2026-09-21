#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ -n "${PYTHON_BIN:-}" ]; then
    PYRAMID_PYTHON="$PYTHON_BIN"
elif [ -x /opt/homebrew/bin/python3 ]; then
    PYRAMID_PYTHON=/opt/homebrew/bin/python3
else
    PYRAMID_PYTHON=python3
fi
"$PYRAMID_PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
"$PYRAMID_PYTHON" -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/ruff check .
if [ ! -e config/okx.credentials.toml ]; then
    (umask 077; cp config/okx.credentials.example.toml config/okx.credentials.toml)
    chmod 600 config/okx.credentials.toml
fi
echo 'Ready. Activate with: source .venv/bin/activate'
