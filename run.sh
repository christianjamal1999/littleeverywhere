#!/bin/sh
# Start KidSong Studio locally at http://127.0.0.1:5178
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
exec .venv/bin/python app.py
