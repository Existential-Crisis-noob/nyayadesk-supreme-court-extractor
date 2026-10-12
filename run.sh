#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_DIR="$PROJECT_DIR/.venv"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Setting up the extractor for first use…"
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
fi

"$VENV_DIR/bin/python" -m pip install --quiet -r "$PROJECT_DIR/requirements.txt"

exec "$VENV_DIR/bin/python" "$PROJECT_DIR/server.py" "$@"
