#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
PYTHONPATH=app python -m compileall -q app
PYTHONPATH=app python -m pytest -q
