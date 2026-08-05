#!/usr/bin/env bash
# Run one AEKE poll and write to InfluxDB. Reads config from ../.env.
# Designed for cron: logs to stdout, exits non-zero on failure.
#
# Optional alerting (Telegram): if ALERT_TELEGRAM_BOT_TOKEN and
# ALERT_TELEGRAM_CHAT_ID are set, sends a message when a poll fails or when the
# token is close to expiry. Expiry warnings are sent once per token (deduped in
# a state file) so you get one nudge, not a daily nag.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${AEKE_ENV_FILE:-$HERE/.env}"
STATE="$HERE/.poll-state"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "run-poll: no env file at $ENV_FILE" >&2
  exit 1
fi
set -a; . "$ENV_FILE"; set +a

NET="${INFLUX_DOCKER_NETWORK:-garmin-grafana_default}"
IMAGE="${AEKE_POLL_IMAGE:-python:3.12-slim}"
WARN_DAYS="${ALERT_WARN_DAYS:-5}"

notify() {  # $1 = message; no-op unless Telegram creds are set
  [[ -n "${ALERT_TELEGRAM_BOT_TOKEN:-}" && -n "${ALERT_TELEGRAM_CHAT_ID:-}" ]] || return 0
  curl -s -m 15 -o /dev/null \
    "https://api.telegram.org/bot${ALERT_TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${ALERT_TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=$1" || true
}

echo "[$(date -Is)] aeke poll starting"

# Days until token expiry (host python; no container needed).
DAYS=$(python3 "$HERE/aeke_export.py" token-info 2>/dev/null | grep -oE '[-0-9.]+ days' | grep -oE '[-0-9.]+' || echo "")
EXP_KEY=$(printf '%s' "${AEKE_TOKEN:-}" | cut -d. -f2 | cut -c1-16)

# Run the poll.
OUT=$(docker run --rm --network "$NET" -v "$HERE:/app:ro" \
  -e AEKE_TOKEN -e AEKE_TZ -e AEKE_BASE -e AEKE_UNITS \
  -e INFLUX_V1_URL -e INFLUX_V1_DB -e INFLUX_V1_USER -e INFLUX_V1_PASSWORD \
  "$IMAGE" python /app/aeke_export.py influx 2>&1)
RC=$?
echo "$OUT"

if [[ $RC -ne 0 ]]; then
  LAST=$(printf '%s' "$OUT" | tail -n1)
  notify "⚠️ AEKE poll failed (rc=$RC): ${LAST}
Likely an expired token. Grab a fresh one from the app and update AEKE_TOKEN in the poller .env. See github.com/batterbob/aeke-data-pull."
  exit $RC
fi

# Proactive one-shot expiry warning.
if [[ -n "$DAYS" ]] && awk "BEGIN{exit !($DAYS <= $WARN_DAYS)}"; then
  if ! grep -qx "expiry:$EXP_KEY" "$STATE" 2>/dev/null; then
    notify "🔑 AEKE token expires in ${DAYS} day(s). Refresh it: open the AEKE app, copy its 'authorization' header, update AEKE_TOKEN in the poller .env."
    echo "expiry:$EXP_KEY" >> "$STATE"
  fi
fi
