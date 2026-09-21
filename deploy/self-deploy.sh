#!/usr/bin/env bash
# self-deploy.sh — the one hard engineering problem in the self-improvement
# loop, solved as a standalone shell script rather than Python inside the
# agent's own process.
#
# /opt/homelab-agent is a live checkout that homelab-agent.service runs
# from. A bad self-edit that gets restarted into takes the agent offline
# permanently, and it cannot fix itself once it's dead - a Python function
# running inside that process cannot rescue a process that just died. So
# the commit -> test -> restart -> verify -> rollback sequence lives here,
# in a script systemd (or a subprocess call from agent/tools/selfops.py)
# invokes directly. Once this script has been started, it does not depend
# on the agent process being alive for any of it - including the rollback.
#
# Usage: self-deploy.sh <path-to-commit-message-file>
#
# Prints progress to stdout for `journalctl`, and always finishes with a
# single line "RESULT_JSON: {...}" as its last line of output - the
# self_deploy tool parses that line and ignores everything before it.
#
# Env overrides (all optional, mainly for tests):
#   SELF_DEPLOY_REPO_DIR      default /opt/homelab-agent
#   SELF_DEPLOY_SERVICE       default homelab-agent
#   SELF_DEPLOY_VERIFY_TIMEOUT_S   default 45  (total time to wait for the
#                             restarted service to prove it's alive)
#   SELF_DEPLOY_VERIFY_INTERVAL_S  default 3
#   SELF_DEPLOY_STABLE_CHECKS      default 3   (consecutive "active" reads
#                             required, spaced VERIFY_INTERVAL_S apart)
set -uo pipefail

REPO_DIR="${SELF_DEPLOY_REPO_DIR:-/opt/homelab-agent}"
SERVICE="${SELF_DEPLOY_SERVICE:-homelab-agent}"
VERIFY_TIMEOUT_S="${SELF_DEPLOY_VERIFY_TIMEOUT_S:-45}"
VERIFY_INTERVAL_S="${SELF_DEPLOY_VERIFY_INTERVAL_S:-3}"
STABLE_CHECKS="${SELF_DEPLOY_STABLE_CHECKS:-3}"
MSG_FILE="${1:-}"

cd "$REPO_DIR" || {
    echo "RESULT_JSON: {\"ok\": false, \"stage\": \"setup\", \"reason\": \"cannot cd to $REPO_DIR\"}"
    exit 1
}

_json_str() {
    # Encode stdin as a JSON string. python3 is already a hard dependency
    # of this checkout (it's what runs the agent), so it's safe to lean on
    # here for correct escaping rather than hand-rolling it in bash.
    python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))'
}

_fail() {
    local stage="$1" reason="$2"
    echo "RESULT_JSON: {\"ok\": false, \"stage\": \"$stage\", \"reason\": $(printf '%s' "$reason" | _json_str)}"
    exit 1
}

echo "== self-deploy starting in $REPO_DIR for service $SERVICE =="

if [ -z "$MSG_FILE" ] || [ ! -f "$MSG_FILE" ]; then
    _fail "setup" "commit message file not found or not given: '$MSG_FILE'"
fi

if [ ! -d .git ]; then
    _fail "setup" "$REPO_DIR is not a git checkout"
fi

PRE_SHA="$(git rev-parse HEAD)"

# Explicit pathspec, never `git add -A` - see the codebase's own git
# discipline in CLAUDE.md/AGENTS-style guidance. Only these paths are ever
# self-authored by the improvement cycle.
if [ -z "$(git status --porcelain -- agent hostctl tests pyproject.toml)" ]; then
    _fail "commit" "no changes staged under agent/hostctl/tests/pyproject.toml - nothing to deploy"
fi

git add -- agent hostctl tests pyproject.toml
if ! git commit -F "$MSG_FILE" >/tmp/self-deploy-commit.log 2>&1; then
    _fail "commit" "git commit failed: $(tail -c 500 /tmp/self-deploy-commit.log)"
fi
NEW_SHA="$(git rev-parse HEAD)"
echo "committed $NEW_SHA (was $PRE_SHA)"

echo "== running the test suite (pytest, ruff, mypy --strict) =="
TEST_LOG=/tmp/self-deploy-tests.log
: >"$TEST_LOG"
TESTS_OK=1
.venv/bin/pytest -q >>"$TEST_LOG" 2>&1 || TESTS_OK=0
if [ "$TESTS_OK" -eq 1 ]; then
    .venv/bin/ruff check . >>"$TEST_LOG" 2>&1 || TESTS_OK=0
fi
if [ "$TESTS_OK" -eq 1 ]; then
    .venv/bin/mypy --strict agent hostctl >>"$TEST_LOG" 2>&1 || TESTS_OK=0
fi

if [ "$TESTS_OK" -ne 1 ]; then
    echo "== test suite failed, reverting $NEW_SHA and NOT restarting =="
    git reset --hard "$PRE_SHA" >/tmp/self-deploy-revert.log 2>&1
    _fail "tests" "test suite failed on $NEW_SHA, reverted to $PRE_SHA: $(tail -c 1500 "$TEST_LOG")"
fi
echo "== tests passed =="

# Best-effort push so the owner can see and revert from GitHub. The
# checkout clones over plain HTTPS with no credential helper configured by
# default (see docs/deploy.md step 4) - GIT_TERMINAL_PROMPT=0 makes a
# missing credential fail in about a second instead of hanging forever
# waiting on a password prompt nobody is there to answer, which would
# otherwise burn the whole cycle's wall-clock budget on this one step.
PUSHED=0
PUSH_LOG=/tmp/self-deploy-push.log
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if GIT_TERMINAL_PROMPT=0 git push origin "$BRANCH" >"$PUSH_LOG" 2>&1; then
    PUSHED=1
    echo "== pushed $NEW_SHA to origin/$BRANCH =="
else
    echo "== push to origin failed (no credential configured?): $(tail -c 300 "$PUSH_LOG") =="
fi

echo "== reinstalling and restarting $SERVICE =="
if ! .venv/bin/pip install -q . >/tmp/self-deploy-install.log 2>&1; then
    git reset --hard "$PRE_SHA" >/tmp/self-deploy-revert.log 2>&1
    .venv/bin/pip install -q . >>/tmp/self-deploy-revert.log 2>&1 || true
    systemctl restart "$SERVICE" || true
    _fail "install" "pip install failed on $NEW_SHA, reverted to $PRE_SHA and restarted: $(tail -c 1000 /tmp/self-deploy-install.log)"
fi

systemctl restart "$SERVICE"

echo "== verifying $SERVICE came back (up to ${VERIFY_TIMEOUT_S}s) =="
elapsed=0
stable=0
while [ "$elapsed" -lt "$VERIFY_TIMEOUT_S" ]; do
    sleep "$VERIFY_INTERVAL_S"
    elapsed=$((elapsed + VERIFY_INTERVAL_S))
    if systemctl is-active --quiet "$SERVICE"; then
        stable=$((stable + 1))
    else
        stable=0
    fi
    echo "  t+${elapsed}s: $(systemctl is-active "$SERVICE" 2>&1) (stable=$stable)"
    if [ "$stable" -ge "$STABLE_CHECKS" ]; then
        break
    fi
done

CRASH_SIGNS="$(journalctl -u "$SERVICE" --since "-${VERIFY_TIMEOUT_S}s" --no-pager 2>/dev/null \
    | grep -iE 'traceback|critical|fatal' | tail -5 || true)"

if [ "$stable" -ge "$STABLE_CHECKS" ] && [ -z "$CRASH_SIGNS" ]; then
    echo "RESULT_JSON: {\"ok\": true, \"stage\": \"done\", \"pre_sha\": \"$PRE_SHA\", \"sha\": \"$NEW_SHA\", \"pushed\": $([ "$PUSHED" -eq 1 ] && echo true || echo false), \"rolled_back\": false}"
    exit 0
fi

echo "== $SERVICE did not come back cleanly, rolling back to $PRE_SHA =="
git reset --hard "$PRE_SHA" >/tmp/self-deploy-revert.log 2>&1
.venv/bin/pip install -q . >>/tmp/self-deploy-revert.log 2>&1 || true
systemctl restart "$SERVICE" || true
sleep "$VERIFY_INTERVAL_S"
ROLLBACK_STATE="$(systemctl is-active "$SERVICE" 2>&1 || true)"

REASON="service did not come back cleanly after deploying $NEW_SHA (stable=$stable/$STABLE_CHECKS, crash_signs=$(printf '%s' "$CRASH_SIGNS" | tr '\n' ' ' | cut -c1-300)); rolled back to $PRE_SHA, restarted, now: $ROLLBACK_STATE"
echo "RESULT_JSON: {\"ok\": false, \"stage\": \"verify\", \"pre_sha\": \"$PRE_SHA\", \"sha\": \"$NEW_SHA\", \"pushed\": $([ "$PUSHED" -eq 1 ] && echo true || echo false), \"rolled_back\": true, \"rollback_state\": \"$ROLLBACK_STATE\", \"reason\": $(printf '%s' "$REASON" | _json_str)}"
exit 1
