#!/usr/bin/env bash
#
# Interactive secrets entry for the homelab agent.
#
# Run this INSIDE LXC 104, as root:
#     pct enter 104
#     bash /opt/homelab-agent/deploy/set-secrets.sh
#
# It prompts for each value, writes /etc/homelab-agent/env with mode 0600, and
# never echoes what you type. Re-running it keeps every existing value unless
# you type a new one, so it is safe to use to rotate a single secret later.
#
# Nothing is written to your shell history, and the file is written through a
# 0600 temporary file in the same directory, so no world-readable copy of your
# tokens ever exists, not even briefly.
#
# Deliberately avoids bash 4 associative arrays so it runs anywhere.

set -euo pipefail

ENV_FILE=/etc/homelab-agent/env
ENV_DIR=$(dirname "$ENV_FILE")

VARS="MINIMAX_API_KEY MINIMAX_MODEL HOSTCTL_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN JELLYFIN_KEY JELLYSEERR_KEY IMMICH_KEY ADGUARD_BASIC_AUTH CALDAV_URL CALDAV_USER CALDAV_PASSWORD"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root — it writes ${ENV_FILE}." >&2
  exit 1
fi

mkdir -p "$ENV_DIR"
chmod 700 "$ENV_DIR"

# Load any existing values into CURRENT_<VAR> shell variables.
if [ -f "$ENV_FILE" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      [A-Z_]*) eval "CURRENT_${key}=\$value" ;;
    esac
  done < "$ENV_FILE"
  echo "Found an existing ${ENV_FILE}. Press Enter at any prompt to keep its current value."
  echo
fi

# ask <VAR> <prompt> <secret|plain> [default]
ask() {
  var=$1
  prompt=$2
  kind=$3
  default=${4:-}

  eval "existing=\${CURRENT_${var}:-\$default}"

  if [ -n "$existing" ]; then
    if [ "$kind" = secret ]; then
      shown=" [keep existing: $(printf '%s' "$existing" | cut -c1-4)…]"
    else
      shown=" [${existing}]"
    fi
  else
    shown=""
  fi

  if [ "$kind" = secret ]; then
    read -r -s -p "${prompt}${shown}: " entered
    echo
  else
    read -r -p "${prompt}${shown}: " entered
  fi

  [ -z "$entered" ] && entered=$existing

  if [ -z "$entered" ]; then
    echo "  ${var} is required and was left empty." >&2
    exit 1
  fi

  eval "ANSWER_${var}=\$entered"
}

cat <<'INTRO'
Homelab agent secrets
=====================
Paste each value when prompted. Secret values are not shown as you type.

INTRO

echo "-- MiniMax (platform.minimax.io -> Token Plan subscription key) --"
ask MINIMAX_API_KEY "MiniMax API key" secret
ask MINIMAX_MODEL   "MiniMax model" plain "MiniMax-M3"

echo
echo "-- hostctl (the token generated on the Proxmox host) --"
echo "   Get it with:  ssh root@10.0.0.2 'cat /etc/hostctl/token'"
ask HOSTCTL_TOKEN "hostctl bearer token" secret

echo
echo "-- Slack (api.slack.com/apps -> your app) --"
ask SLACK_BOT_TOKEN "Slack bot token (xoxb-…)" secret
ask SLACK_APP_TOKEN "Slack app token (xapp-…)" secret

echo
echo "-- Media and photos (each service's own settings page) --"
ask JELLYFIN_KEY       "Jellyfin API key" secret
ask JELLYSEERR_KEY     "Jellyseerr API key" secret
ask IMMICH_KEY         "Immich API key" secret
ask ADGUARD_BASIC_AUTH "AdGuard basic-auth value (base64 of user:password)" secret

echo
echo "-- Family calendar (Radicale) --"
ask CALDAV_URL      "CalDAV URL" plain "http://10.0.0.165:5232/family/home/"
ask CALDAV_USER     "CalDAV username" plain "family"
ask CALDAV_PASSWORD "CalDAV password" secret

TMP=$(mktemp "${ENV_DIR}/.env.XXXXXX")
chmod 600 "$TMP"
trap 'rm -f "$TMP"' EXIT

{
  echo "# Written by deploy/set-secrets.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)."
  echo "# Mode 0600. Never commit this file or paste its contents anywhere."
  for var in $VARS; do
    eval "value=\$ANSWER_${var}"
    printf '%s=%s\n' "$var" "$value"
  done
} > "$TMP"

mv "$TMP" "$ENV_FILE"
trap - EXIT
chmod 600 "$ENV_FILE"

echo
echo "Wrote ${ENV_FILE} — $(grep -c '=' "$ENV_FILE") values, mode 0600."
echo
echo "Next:"
echo "  systemctl restart homelab-agent"
echo "  systemctl is-active homelab-agent"
echo "  journalctl -u homelab-agent -n 30 --no-pager"
echo
echo "A successful start posts ':satellite: homelab agent online' to #homelab."
