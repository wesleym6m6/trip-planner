#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-$PWD/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

echo "== trip-planner: python compile =="
"$PYTHON" -m py_compile scripts/*.py

if [ -d trips ]; then
  echo "== trip-planner: validate trips =="
  found=0
  for trip_dir in trips/*; do
    if [ -d "$trip_dir/data" ]; then
      found=1
      "$PYTHON" scripts/validate_trip.py "$trip_dir"
    fi
  done
  if [ "$found" -eq 0 ]; then
    echo "No trip data directories found under trips/*/data; skipping trip validation."
  fi
else
  echo "No trips/ directory found; skipping trip validation."
fi

echo "== trip-planner: check OK =="
