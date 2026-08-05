#!/usr/bin/env bash
# Manually open a token-refresh window right now (instead of waiting for the
# poller to trigger one near expiry). Runs in the foreground.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/refresh_window.py"
