#!/usr/bin/env bash
# homelab-watchdog.sh — runs on the Proxmox host itself (10.0.0.2), driven
# by deploy/homelab-watchdog.timer, and does not depend on the agent
# process, LXC 104's container, or even homelab-agent.service's own
# `OnFailure=` alert (deploy/homelab-agent-alert@.service) being able to
# run. The host outlives the container: if LXC 104 itself is stopped,
# rebooting, or gone, the in-container OnFailure alert can't fire either
# (systemd inside a dead container isn't running), and this is the only
# thing left that notices.
#
# Two things are checked each run:
#   1. The container/service is reachable and reports `active`
#      (`pct exec 104 -- systemctl is-active homelab-agent`) - `pct exec`
#      itself failing (container stopped, host can't reach it) counts as
#      down too, not as "unknown".
#   2. The agent's own journal has produced *something* in the last
#      `WATCHDOG_STALE_MINUTES` minutes. This is a proxy for "still
#      talking to Slack, not just still resident in memory": there's no
#      periodic heartbeat log line to check for specifically (nothing
#      changed under agent/ for this task), so silence in the journal
#      while `systemctl is-active` still reports active is exactly the
#      case that check alone would miss - a wedged process that never
#      logs again after its last successful Slack call. See docs/deploy.md
#      for this tradeoff spelled out.
#
# Token source: `/etc/homelab-watchdog/env` on the host, populated once
# (and again on rotation) by copying `SLACK_BOT_TOKEN` out of the agent's
# own `/etc/homelab-agent/env` - not read fresh via `pct exec` on every
# run. That's the deliberate choice for this script specifically (see
# docs/deploy.md): `pct exec` needs the container up, and the one case
# this script exists for that the in-container alert unit cannot cover is
# exactly "the container itself is down" - a token source that also needs
# the container up would fail to notify in precisely that case.
#
# State (`/var/lib/homelab-watchdog/state`) tracks only the last observed
# health and when it last changed, so alerts fire on the transition into
# or out of a bad state, not on every run while it stays broken.
#
# Env overrides (all optional, mainly for tests):
#   HOMELAB_WATCHDOG_ENV_FILE      default /etc/homelab-watchdog/env
#   HOMELAB_WATCHDOG_STATE_FILE    default /var/lib/homelab-watchdog/state
#   HOMELAB_WATCHDOG_CT            default 104
#   HOMELAB_WATCHDOG_SERVICE       default homelab-agent
#   HOMELAB_WATCHDOG_STALE_MINUTES default 15
#   HOMELAB_WATCHDOG_JOURNAL_LINES default 10
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${HOMELAB_WATCHDOG_ENV_FILE:-/etc/homelab-watchdog/env}"
STATE_FILE="${HOMELAB_WATCHDOG_STATE_FILE:-/var/lib/homelab-watchdog/state}"
CT="${HOMELAB_WATCHDOG_CT:-104}"
SERVICE="${HOMELAB_WATCHDOG_SERVICE:-homelab-agent}"
STALE_MINUTES="${HOMELAB_WATCHDOG_STALE_MINUTES:-15}"
JOURNAL_LINES="${HOMELAB_WATCHDOG_JOURNAL_LINES:-10}"

if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a
    source "$ENV_FILE"
    set +a
fi

mkdir -p "$(dirname "$STATE_FILE")"

_now() { date +%s; }

# --- gather evidence -------------------------------------------------
# `systemctl is-active` exits non-zero for any non-"active" state (3 for
# inactive/failed) while still printing that state to stdout - so exit
# status alone can't distinguish "pct reached the container and the
# service just isn't active" from "pct itself couldn't reach the
# container at all". Empty stdout is what actually means the latter: `pct
# exec` failing outright (container stopped, host can't reach it) prints
# its own error to stderr and nothing to stdout.
UNREACHABLE=0
IS_ACTIVE="$(pct exec "$CT" -- systemctl is-active "$SERVICE" 2>/tmp/homelab-watchdog-pct.err)"
if [ -z "$IS_ACTIVE" ]; then
    UNREACHABLE=1
fi

JOURNAL_TAIL=""
JOURNAL_RECENT_LINES=0
if [ "$UNREACHABLE" -eq 0 ]; then
    JOURNAL_TAIL="$(pct exec "$CT" -- journalctl -u "$SERVICE" -n "$JOURNAL_LINES" --no-pager 2>/dev/null)"
    JOURNAL_RECENT_LINES="$(pct exec "$CT" -- journalctl -u "$SERVICE" --since "-${STALE_MINUTES}min" --no-pager -q 2>/dev/null | wc -l | tr -d ' ')"
fi

# --- decide health -----------------------------------------------------
HEALTHY=1
REASON=""
if [ "$UNREACHABLE" -eq 1 ]; then
    HEALTHY=0
    REASON="pct exec into CT $CT failed - container may be stopped or unreachable: $(cat /tmp/homelab-watchdog-pct.err 2>/dev/null | tr -d '\n' | cut -c1-300)"
elif [ "$IS_ACTIVE" != "active" ]; then
    HEALTHY=0
    REASON="$SERVICE is not active on CT $CT (systemctl is-active: $IS_ACTIVE)"
elif [ "${JOURNAL_RECENT_LINES:-0}" -eq 0 ]; then
    HEALTHY=0
    REASON="$SERVICE is active but has produced no journal output in the last ${STALE_MINUTES}m - possibly wedged"
fi

# --- load previous state, compare, act on transitions only -------------
PREV_STATUS="unknown"
NOW="$(_now)"
LAST_OK_AT=""
if [ -f "$STATE_FILE" ]; then
    # shellcheck disable=SC1090
    source "$STATE_FILE"
fi

NEW_STATUS="ok"
[ "$HEALTHY" -eq 1 ] || NEW_STATUS="down"

# LAST_OK_AT only ever advances on an "ok" reading - it's the newest time
# this watchdog can vouch for the service being healthy, so on a down
# transition "NOW - LAST_OK_AT" is a real lower bound on how long it's been
# down (exact if this is the first bad check after a run of good ones;
# understated only by however stale the very first state file is).
if [ "$NEW_STATUS" = "ok" ]; then
    LAST_OK_AT="$NOW"
fi

{
    echo "PREV_STATUS=$NEW_STATUS"
    echo "LAST_OK_AT=$LAST_OK_AT"
} >"$STATE_FILE"

if [ "$NEW_STATUS" = "$PREV_STATUS" ]; then
    echo "homelab-watchdog: no change ($NEW_STATUS)"
    exit 0
fi

if [ "$PREV_STATUS" = "unknown" ] && [ "$NEW_STATUS" = "ok" ]; then
    # First run ever (no state file yet) and everything looks fine: this is
    # baseline seeding, not a recovery to announce - alerting here would
    # mean every fresh install or state-file wipe posts a spurious "it's
    # back" the first time the watchdog runs at all.
    echo "homelab-watchdog: first observation is healthy, seeding state silently"
    exit 0
fi

if [ "$NEW_STATUS" = "down" ]; then
    if [ -z "$LAST_OK_AT" ]; then
        DOWNTIME_NOTE="no prior healthy reading recorded (first watchdog run)"
    else
        DOWN_FOR_S=$((NOW - LAST_OK_AT))
        LAST_OK_ISO="$(date -u -d "@$LAST_OK_AT" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$LAST_OK_AT" +%Y-%m-%dT%H:%M:%SZ)"
        DOWNTIME_NOTE="down for at least ${DOWN_FOR_S}s - last confirmed healthy $LAST_OK_ISO"
    fi
    MESSAGE=":rotating_light: *$SERVICE* looks down on CT $CT ($REASON). ${DOWNTIME_NOTE}.

Last $JOURNAL_LINES journal lines:
\`\`\`
$JOURNAL_TAIL
\`\`\`

Look further: \`ssh root@10.0.0.2 'pct exec $CT -- journalctl -u $SERVICE -n 200'\` or \`ssh root@10.0.0.2 'pct exec $CT -- systemctl status $SERVICE'\`."
    echo "homelab-watchdog: transition ok -> down, alerting"
else
    MESSAGE=":white_check_mark: *$SERVICE* on CT $CT is back."
    echo "homelab-watchdog: transition down -> ok, alerting"
fi

SLACK_CHANNEL="${SLACK_CHANNEL:-#homelab-alerts}" "$SCRIPT_DIR/notify-slack.sh" "$MESSAGE"
