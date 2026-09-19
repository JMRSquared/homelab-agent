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

VARS="MINIMAX_API_KEY MINIMAX_MODEL HOSTCTL_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_REPLY_WITHOUT_MENTION SLACK_CHANNEL_STATUS SLACK_CHANNEL_LOG JELLYFIN_KEY JELLYSEERR_KEY IMMICH_KEY ADGUARD_BASIC_AUTH UPTIME_KUMA_SLUG AGENT_TICK_SECONDS MAIL_PASSWORD MAIL_SMTP_HOST MAIL_SMTP_PORT MAIL_FROM"

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

# ask <VAR> <prompt> <secret|plain> [default] [optional]
#
# A variable marked "optional" may be left blank. The agent starts without it;
# only the tools that need it fail, and they fail as a typed error the model
# sees and can report, not as a crash. Required variables are the four the
# process cannot start without: the model key, the hostctl token, and the two
# Slack tokens.
ask() {
  var=$1
  prompt=$2
  kind=$3
  default=${4:-}
  optional=${5:-}

  eval "existing=\${CURRENT_${var}:-\$default}"

  if [ -n "$existing" ]; then
    if [ "$kind" = secret ]; then
      shown=" [keep existing]"
    else
      shown=" [${existing}]"
    fi
  elif [ -n "$optional" ]; then
    shown=" [Enter to skip]"
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

  if [ -z "$entered" ] && [ -z "$optional" ]; then
    echo "  ${var} is required — the agent cannot start without it." >&2
    exit 1
  fi

  if [ -z "$entered" ]; then
    SKIPPED="${SKIPPED} ${var}"
  fi

  eval "ANSWER_${var}=\$entered"
}

SKIPPED=""

cat <<'INTRO'
Homelab agent secrets
=====================
Paste each value when prompted. Secret values are not shown as you type.

Four values are required, because the agent process cannot start without them:
the MiniMax key, the hostctl token, and the two Slack tokens.

Everything else can be left blank by pressing Enter. The agent still starts;
only the tools needing that value are unavailable, and they report a clear
error rather than crashing. Re-run this script later to fill them in.

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
ask SLACK_REPLY_WITHOUT_MENTION "Reply in channels without an @mention? (1=yes, 0=mention required; see docs/slack-setup.md step 5)" plain "1" optional
ask SLACK_CHANNEL_STATUS "Status/incident channel (see docs/slack-setup.md step 6)" plain "#homelab-alerts" optional
ask SLACK_CHANNEL_LOG    "Tool-call audit log channel (see docs/slack-setup.md step 6)" plain "#homelab-agent-log" optional

echo
echo "-- Media, photos and DNS — all optional, press Enter to skip any --"
ask JELLYFIN_KEY       "Jellyfin API key" secret "" optional
ask JELLYSEERR_KEY     "Jellyseerr API key" secret "" optional
ask IMMICH_KEY         "Immich API key" secret "" optional
ask ADGUARD_BASIC_AUTH "AdGuard basic-auth value (base64 of user:password)" secret "" optional
ask UPTIME_KUMA_SLUG   "Uptime Kuma status page slug (see docs/deploy.md step 5)" plain "homelab" optional

echo
echo "-- Mail (send_email) - MAIL_PASSWORD is likely not available yet; press Enter to skip --"
ask MAIL_PASSWORD  "Password for admin@mail.jmrsquared.com" secret "" optional
ask MAIL_SMTP_HOST "Mail server host (Stalwart, LXC 103)" plain "10.0.0.167" optional
ask MAIL_SMTP_PORT "Mail server SMTP-over-implicit-TLS port" plain "465" optional
ask MAIL_FROM      "Send-as address" plain "admin@mail.jmrsquared.com" optional

echo
echo "-- Autonomous tick (see docs/deploy.md step 8) --"
ask AGENT_TICK_SECONDS "Seconds between autonomous sweeps; 0 disables the tick and leaves only Slack (incident lever, leave at 60 for normal running)" plain "60" optional

TMP=$(mktemp "${ENV_DIR}/.env.XXXXXX")
chmod 600 "$TMP"
trap 'rm -f "$TMP"' EXIT

{
  echo "# Written by deploy/set-secrets.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)."
  echo "# Mode 0600. Never commit this file or paste its contents anywhere."
  for var in $VARS; do
    eval "value=\$ANSWER_${var}"
    # Omit skipped values entirely rather than writing an empty string. The
    # tools read os.environ["KEY"], so an absent variable raises a clear
    # KeyError that dispatch turns into a typed error naming the variable,
    # whereas an empty one would reach the service as a blank credential and
    # come back as a confusing 401.
    [ -n "$value" ] && printf '%s=%s\n' "$var" "$value"
  done
} > "$TMP"

mv "$TMP" "$ENV_FILE"
trap - EXIT
chmod 600 "$ENV_FILE"

echo
echo "Wrote ${ENV_FILE} — $(grep -cv '^#' "$ENV_FILE") values, mode 0600."

if [ -n "$SKIPPED" ]; then
  echo
  echo "Skipped:${SKIPPED}"
  echo
  echo "The agent will start. These tools stay unavailable until you re-run"
  echo "this script and fill the matching value in:"
  for var in $SKIPPED; do
    case "$var" in
      JELLYFIN_KEY)       echo "  media_search, media_library_status" ;;
      JELLYSEERR_KEY)     echo "  media_request" ;;
      IMMICH_KEY)         echo "  photos_search, photos_stats, photos_download" ;;
      ADGUARD_BASIC_AUTH) echo "  adguard_report" ;;
      MAIL_PASSWORD)      echo "  send_email" ;;
    esac
  done | sort -u
  echo
  echo "Asking the agent for one of those gets a clear error naming the missing"
  echo "variable. Nothing crashes, and every other tool keeps working."
fi

echo
echo "Next:"
echo "  systemctl restart homelab-agent"
echo "  systemctl is-active homelab-agent"
echo "  journalctl -u homelab-agent -n 30 --no-pager"
echo
echo "A successful start posts ':satellite: homelab agent online' to #homelab."
