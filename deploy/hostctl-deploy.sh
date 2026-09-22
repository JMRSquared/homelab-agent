#!/usr/bin/env bash
# hostctl-deploy.sh — self-deploy.sh's rollback discipline, applied to
# hostctl.
#
# /opt/hostctl used to be an rsync/cp target: nothing recorded what was
# running there, and nothing could put back what was running before a bad
# deploy. hostctl is what grants the agent (and an operator) host-level
# control in the first place, so a bad deploy there takes away the hands
# the rest of this system depends on, with no automatic way back - the one
# thing self-deploy.sh already solved for the agent itself.
#
# /opt/hostctl is now a checkout of this same repo (see docs/deploy.md for
# the one-time migration from the old copied directory). This script:
# record the current commit, fast-forward pull, reinstall, restart, verify
# with a real authenticated HTTP request (not just `systemctl is-active` -
# a process that starts and then 500s on every call is the case that
# matters), and roll back to the recorded commit and restart if that
# verification doesn't come back clean.
#
# Verification deliberately does not curl hostctl from the host's own
# shell: docs/deploy.md 1a's nftables rule filters 10.0.0.2:8710 even for
# traffic that originates on the host itself (loopback bind isn't
# available - see deploy/hostctl.service). The one address the firewall
# allows is the agent's own LXC (10.0.0.168), so verification runs the
# curl there via `pct exec`, from a host that already has `pct` for every
# other guest-touching step in this project.
#
# Usage: hostctl-deploy.sh
#
# Prints progress to stdout for `journalctl`, and always finishes with a
# single line "RESULT_JSON: {...}" as its last line of output.
#
# Env overrides (all optional, mainly for tests):
#   HOSTCTL_DEPLOY_REPO_DIR        default /opt/hostctl
#   HOSTCTL_DEPLOY_SERVICE         default hostctl
#   HOSTCTL_DEPLOY_TOKEN_FILE      default /etc/hostctl/token
#   HOSTCTL_DEPLOY_VERIFY_GUEST    default 104 (pct exec target the curl runs from)
#   HOSTCTL_DEPLOY_VERIFY_URL      default http://10.0.0.2:8710/guests
#   HOSTCTL_DEPLOY_VERIFY_TIMEOUT_S    default 30 (total time to wait for a clean verify)
#   HOSTCTL_DEPLOY_VERIFY_INTERVAL_S   default 3
#   HOSTCTL_DEPLOY_STABLE_CHECKS       default 2 (consecutive HTTP 200s required)
set -uo pipefail

REPO_DIR="${HOSTCTL_DEPLOY_REPO_DIR:-/opt/hostctl}"
SERVICE="${HOSTCTL_DEPLOY_SERVICE:-hostctl}"
TOKEN_FILE="${HOSTCTL_DEPLOY_TOKEN_FILE:-/etc/hostctl/token}"
VERIFY_GUEST="${HOSTCTL_DEPLOY_VERIFY_GUEST:-104}"
VERIFY_URL="${HOSTCTL_DEPLOY_VERIFY_URL:-http://10.0.0.2:8710/guests}"
VERIFY_TIMEOUT_S="${HOSTCTL_DEPLOY_VERIFY_TIMEOUT_S:-30}"
VERIFY_INTERVAL_S="${HOSTCTL_DEPLOY_VERIFY_INTERVAL_S:-3}"
STABLE_CHECKS="${HOSTCTL_DEPLOY_STABLE_CHECKS:-2}"

cd "$REPO_DIR" || {
    echo "RESULT_JSON: {\"ok\": false, \"stage\": \"setup\", \"reason\": \"cannot cd to $REPO_DIR\"}"
    exit 1
}

_json_str() {
    python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))'
}

_fail() {
    local stage="$1" reason="$2"
    echo "RESULT_JSON: {\"ok\": false, \"stage\": \"$stage\", \"reason\": $(printf '%s' "$reason" | _json_str)}"
    exit 1
}

echo "== hostctl-deploy starting in $REPO_DIR for service $SERVICE =="

if [ ! -d .git ]; then
    _fail "setup" "$REPO_DIR is not a git checkout - see docs/deploy.md for the migration"
fi

if [ -n "$(git status --porcelain)" ]; then
    _fail "setup" "$REPO_DIR has local changes - hostctl is deploy-only, nothing should edit it in place"
fi

PRE_SHA="$(git rev-parse HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

echo "== fetching origin/$BRANCH =="
PULL_LOG=/tmp/hostctl-deploy-pull.log
if ! git fetch origin "$BRANCH" >"$PULL_LOG" 2>&1; then
    _fail "pull" "git fetch origin $BRANCH failed: $(tail -c 500 "$PULL_LOG")"
fi
if ! git merge --ff-only "origin/$BRANCH" >>"$PULL_LOG" 2>&1; then
    _fail "pull" "not a fast-forward from $PRE_SHA to origin/$BRANCH: $(tail -c 500 "$PULL_LOG")"
fi
NEW_SHA="$(git rev-parse HEAD)"
echo "pulled $NEW_SHA (was $PRE_SHA)"

_rollback() {
    local reason_stage="$1"
    echo "== rolling back to $PRE_SHA =="
    git reset --hard "$PRE_SHA" >/tmp/hostctl-deploy-revert.log 2>&1
    .venv/bin/pip install -q . >>/tmp/hostctl-deploy-revert.log 2>&1 || true
    systemctl restart "$SERVICE" || true
}

echo "== reinstalling into $REPO_DIR/.venv =="
# hostctl's venv now installs from the same pyproject.toml the agent does
# ("its own venv as today" - just sourced from a git checkout instead of a
# hand-picked package list). That does pull in the agent's own runtime
# dependencies alongside fastapi/uvicorn, which the old `pip install
# fastapi uvicorn` did not - an accepted, documented cost (see
# docs/deploy.md) of having one source of truth for dependencies instead of
# a second hand-maintained list that can drift from pyproject.toml, the
# same tradeoff self-deploy.sh already makes for the agent itself.
INSTALL_LOG=/tmp/hostctl-deploy-install.log
if ! .venv/bin/pip install -q . >"$INSTALL_LOG" 2>&1; then
    _rollback "install"
    _fail "install" "pip install failed on $NEW_SHA, reverted to $PRE_SHA and restarted: $(tail -c 1000 "$INSTALL_LOG")"
fi

echo "== restarting $SERVICE =="
systemctl restart "$SERVICE"

echo "== verifying $SERVICE with an authenticated request (up to ${VERIFY_TIMEOUT_S}s) =="
if [ ! -f "$TOKEN_FILE" ]; then
    _rollback "verify"
    _fail "verify" "token file $TOKEN_FILE not found, rolled back to $PRE_SHA and restarted"
fi
TOKEN="$(cat "$TOKEN_FILE")"

elapsed=0
stable=0
last_code="-"
while [ "$elapsed" -lt "$VERIFY_TIMEOUT_S" ]; do
    sleep "$VERIFY_INTERVAL_S"
    elapsed=$((elapsed + VERIFY_INTERVAL_S))
    last_code="$(pct exec "$VERIFY_GUEST" -- curl -s -o /dev/null -m 5 -w '%{http_code}' \
        -H "Authorization: Bearer $TOKEN" "$VERIFY_URL" 2>/dev/null || echo "000")"
    if [ "$last_code" = "200" ]; then
        stable=$((stable + 1))
    else
        stable=0
    fi
    echo "  t+${elapsed}s: HTTP $last_code (stable=$stable)"
    if [ "$stable" -ge "$STABLE_CHECKS" ]; then
        break
    fi
done

if [ "$stable" -ge "$STABLE_CHECKS" ]; then
    echo "RESULT_JSON: {\"ok\": true, \"stage\": \"done\", \"pre_sha\": \"$PRE_SHA\", \"sha\": \"$NEW_SHA\", \"rolled_back\": false}"
    exit 0
fi

echo "== $SERVICE did not answer cleanly (last HTTP $last_code), rolling back to $PRE_SHA =="
_rollback "verify"
sleep "$VERIFY_INTERVAL_S"
ROLLBACK_CODE="$(pct exec "$VERIFY_GUEST" -- curl -s -o /dev/null -m 5 -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" "$VERIFY_URL" 2>/dev/null || echo "000")"

REASON="hostctl did not answer with HTTP 200 after deploying $NEW_SHA (last_code=$last_code, stable=$stable/$STABLE_CHECKS); rolled back to $PRE_SHA, restarted, now: HTTP $ROLLBACK_CODE"
echo "RESULT_JSON: {\"ok\": false, \"stage\": \"verify\", \"pre_sha\": \"$PRE_SHA\", \"sha\": \"$NEW_SHA\", \"rolled_back\": true, \"rollback_http_code\": \"$ROLLBACK_CODE\", \"reason\": $(printf '%s' "$REASON" | _json_str)}"
exit 1
