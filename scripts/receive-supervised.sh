#!/usr/bin/env bash
# Fixed root-only SSH forced command; stdin is the existing bounded JSON transport.
# Never eval SSH_ORIGINAL_COMMAND or forward caller command-line arguments.
set -euo pipefail
[[ $# == 0 && $EUID == 0 ]] || exit 2
exec /usr/bin/systemd-run --quiet --pipe --wait --collect \
  --unit=map-deploy-receive.service --service-type=exec \
  --property=KillMode=control-group --property=TimeoutStopSec=10 \
  --property=SendSIGKILL=yes --property=RuntimeMaxSec=5400 \
  --property=UMask=0077 \
  /usr/bin/python3 /usr/local/lib/map-deploy/deploy-gcp.py receive
