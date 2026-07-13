#!/usr/bin/env bash
#
#   ./run.sh                   # OWNER mode: Penny browses the data plane as the owner.
#   ./run.sh <consumer-token>  # CONSUMER mode: Penny browses the /.engine/ surface as that
#                              #   grant (open http://localhost:9000/.engine/discovery).

set -euo pipefail

ADMIN_TOKEN="${LDP_ADMIN_TOKEN:-dev-secret}"        # boots the pod (owner secret)
CONSUMER_ARG="${1:-${CONSUMER_TOKEN:-}}"            # optional: browse as this consumer
PROXY_PORT="${PROXY_PORT:-9000}"
POD_PORT="${UPSTREAM_PORT:-8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$HERE/.."   # this directory lives inside the backend repo

if [ -n "$CONSUMER_ARG" ]; then
  INJECT_TOKEN="$CONSUMER_ARG"; MODE="CONSUMER"; ENTRY="http://localhost:$PROXY_PORT/.engine/discovery"
else
  INJECT_TOKEN="$ADMIN_TOKEN";  MODE="OWNER";    ENTRY="http://localhost:$PROXY_PORT/"
fi

echo "Starting pod on :$POD_PORT (base URI http://localhost:$PROXY_PORT/) ..."
( cd "$BACKEND" && LDP_ADMIN_TOKEN="$ADMIN_TOKEN" \
    LDP_BASE_URI="http://localhost:$PROXY_PORT/" \
    LDP_PORT="$POD_PORT" \
    uv run python -m ldp_personal_store.main ) &
POD_PID=$!
trap 'kill "$POD_PID" 2>/dev/null || true' EXIT

echo "Waiting for the pod to be ready ..."
until curl -sf -o /dev/null "http://127.0.0.1:$POD_PORT/health"; do sleep 0.5; done
echo "Pod is up."

echo
echo "=================================================================="
echo "  Admin token:    $ADMIN_TOKEN"
if [ "$MODE" = CONSUMER ]; then
  echo "  Consumer token: $CONSUMER_ARG"
fi
echo "  ----------------------------------------------------------------"
echo "  Mode: $MODE   ->  open Penny at:  $ENTRY"
if [ "$MODE" = OWNER ]; then
  echo "  Browse as a consumer with:  ./run.sh <consumer-token>"
fi
echo "=================================================================="
echo

echo "Starting proxy on :$PROXY_PORT  [$MODE mode]"
INJECT_TOKEN="$INJECT_TOKEN" PROXY_PORT="$PROXY_PORT" UPSTREAM_PORT="$POD_PORT" \
  exec python "$HERE/proxy.py"
