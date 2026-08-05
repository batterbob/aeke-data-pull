#!/usr/bin/env bash
# Run one AEKE poll and write to InfluxDB. Reads config from ../.env.
# Designed for cron. Logs to stdout; exits non-zero on failure so cron mail fires.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${AEKE_ENV_FILE:-$HERE/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "run-poll: no env file at $ENV_FILE" >&2
  exit 1
fi
set -a; . "$ENV_FILE"; set +a

NET="${INFLUX_DOCKER_NETWORK:-garmin-grafana_default}"
IMAGE="${AEKE_POLL_IMAGE:-python:3.12-slim}"

echo "[$(date -Is)] aeke poll starting"
exec docker run --rm --network "$NET" -v "$HERE:/app:ro" \
  -e AEKE_TOKEN -e AEKE_TZ -e AEKE_BASE -e AEKE_UNITS \
  -e INFLUX_V1_URL -e INFLUX_V1_DB -e INFLUX_V1_USER -e INFLUX_V1_PASSWORD \
  "$IMAGE" python /app/aeke_export.py influx
