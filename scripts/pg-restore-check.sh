#!/usr/bin/env bash
# Explicit --test or --prod; restore also requires a bundle manifest path.
set -euo pipefail
exec python3 "$(dirname "$0")/pg_backup.py" restore "$@"
