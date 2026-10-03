#!/bin/sh
# Le rapport tourne sous uv depuis la racine du projet (aw_fix_gaps / aw_log y sont importés).
cd /home/hugoc/Documents/git2/aw-report || exit 1
uv run aw-report.py "$@"
