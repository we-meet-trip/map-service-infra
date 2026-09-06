#!/usr/bin/env bash
# Safe compatibility entry point. A fixed-date source, provider checksum, new
# build volume, and output directory are mandatory. Existing serving volumes
# are never accepted as build targets. See docs/OSRM_RUNTIME_RELEASE.md.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/osrm-release.py" build "$@"
