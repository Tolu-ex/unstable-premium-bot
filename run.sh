#!/usr/bin/env bash
# Loads .env and starts the watcher.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "No .env found. Copy .env.example to .env and fill in your bot token." >&2
  exit 1
fi

set -a; . ./.env; set +a
exec "${PYTHON:-/usr/bin/python3}" unstable_alert.py
