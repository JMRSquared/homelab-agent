#!/usr/bin/env bash
# notify-slack.sh — posts one message to Slack via `curl` and a bot token,
# with no dependency on the agent process, the agent's Python, or even the
# agent's container being reachable. Used by both:
#   - deploy/homelab-agent-alert.service (inside LXC 104, triggered by
#     `OnFailure=` on homelab-agent.service - runs even though the thing
#     that just failed is the agent's own process)
#   - deploy/homelab-watchdog.sh (on the Proxmox host itself, which
#     outlives the container - the check that notices the agent going
#     silent even if the container is stopped or gone, not just the
#     service inside it)
#
# Slack is deliberately reached directly here, never through the agent -
# an agent that's dead, wedged, or crash-looping cannot be the thing that
# reports its own failure.
#
# Usage: notify-slack.sh "<message text>"
# Reads SLACK_BOT_TOKEN (required) and SLACK_CHANNEL (optional, default
# #homelab-alerts) from the environment - both callers above set these via
# their own EnvironmentFile=, so nothing here parses any file itself.
set -uo pipefail

MESSAGE="${1:-}"
CHANNEL="${SLACK_CHANNEL:-#homelab-alerts}"

if [ -z "$MESSAGE" ]; then
    echo "notify-slack.sh: usage: notify-slack.sh '<message text>'" >&2
    exit 2
fi

if [ -z "${SLACK_BOT_TOKEN:-}" ]; then
    echo "notify-slack.sh: SLACK_BOT_TOKEN is not set, cannot post: $MESSAGE" >&2
    exit 1
fi

# --data-urlencode handles JSON-unsafe characters (quotes, newlines) in
# $MESSAGE without hand-rolled escaping; -sS surfaces curl's own errors
# (DNS, TLS, timeout) on stderr while staying quiet on success.
RESPONSE="$(curl -sS -m 10 -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    --data-urlencode "channel=$CHANNEL" \
    --data-urlencode "text=$MESSAGE")"
CURL_STATUS=$?

if [ "$CURL_STATUS" -ne 0 ]; then
    echo "notify-slack.sh: curl failed (exit $CURL_STATUS) posting to $CHANNEL: $MESSAGE" >&2
    exit 1
fi

# Slack's chat.postMessage answers HTTP 200 even on failure (bad token,
# channel_not_found, not_in_channel, ...) with {"ok": false, "error": "..."}
# in the body - only the body tells you whether it actually worked.
if ! printf '%s' "$RESPONSE" | grep -q '"ok":true'; then
    echo "notify-slack.sh: Slack rejected the post to $CHANNEL: $RESPONSE" >&2
    exit 1
fi

exit 0
