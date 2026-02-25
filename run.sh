#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec uv run --python /opt/homebrew/bin/python3.14 "$DIR/app.py"
