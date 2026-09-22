#!/usr/bin/env bash
# homelab-agent-alert.sh — runs inside LXC 104, triggered by
# `OnFailure=homelab-agent-alert.service` on homelab-agent.service itself
# (see deploy/homelab-agent.service). systemd starts this as a brand new
# process when homelab-agent.service enters a failed state, so it runs
# regardless of whether the agent's own process is alive, wedged, or gone -
# it does not import or call anything from the agent's Python at all, and
# posts to Slack directly via deploy/notify-slack.sh and curl.
#
# Usage: homelab-agent-alert.sh <failed-unit-name>
# (deploy/homelab-agent-alert.service passes systemd's own %n)
#
# Reads SLACK_BOT_TOKEN and SLACK_CHANNEL_STATUS from the environment - the
# unit sets EnvironmentFile=/etc/homelab-agent/env, the same file
# homelab-agent.service itself uses, so no secret is duplicated anywhere
# else on disk for this to work.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="${1:-homelab-agent.service}"
JOURNAL_LINES="${HOMELAB_AGENT_ALERT_JOURNAL_LINES:-15}"

JOURNAL_TAIL="$(journalctl -u "$UNIT" -n "$JOURNAL_LINES" --no-pager 2>/dev/null \
    | tail -c 1500)"

MESSAGE=":rotating_light: *$UNIT has failed* on LXC 104.

Last $JOURNAL_LINES journal lines:
\`\`\`
$JOURNAL_TAIL
\`\`\`

Look further: \`ssh root@10.0.0.2 'pct exec 104 -- journalctl -u $UNIT -n 200'\` or \`ssh root@10.0.0.2 'pct exec 104 -- systemctl status $UNIT'\`."

SLACK_CHANNEL="${SLACK_CHANNEL_STATUS:-#homelab-alerts}" "$SCRIPT_DIR/notify-slack.sh" "$MESSAGE"
