"""Tools for the agent to safely modify and redeploy its own source, and to
restart hostctl, its own root-on-host boundary.

Both actions here share one shape the rest of this codebase doesn't need
elsewhere: an action that can take the thing doing the acting offline.
`guest_exec`/`host_exec` (agent/tools/infra.py) already let the model edit
files and run arbitrary commands, including `git commit` and even
`systemctl restart homelab-agent` directly - nothing stops the model from
bypassing these tools and doing it the unsafe way. What these tools add is
the *safe* way: commit, test, restart, verify the restart actually worked,
and roll back automatically if it didn't - all engineering, not an
approval gate (the owner asked for neither). `agent/improve.py`'s system
prompt tells the model to prefer these over a raw restart for exactly that
reason.
"""

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from agent.clients import EXEC_TIMEOUT, hostctl_get, hostctl_post
from agent.tools.base import tool

logger = logging.getLogger(__name__)

_SELF_DEPLOY_SCRIPT = Path(__file__).resolve().parent.parent.parent / "deploy" / "self-deploy.sh"
# Generous: pytest + ruff + mypy --strict on this codebase, plus a pip
# reinstall and a restart-and-verify window, comfortably fits inside this
# but a genuinely hung test or install must not hang the whole improvement
# cycle forever - see agent/improve.py's own wall-clock timeout, which this
# sits inside of.
_SELF_DEPLOY_TIMEOUT_S = 480.0

# hostctl has no git checkout in this tool's scope (it's deployed by
# rsync/scp per docs/deploy.md, not `git pull`), so there is no code-level
# rollback available for it the way there is for the agent's own checkout.
# This is a real gap, not an oversight - see the self-improve report.
_HOSTCTL_VERIFY_TIMEOUT_S = 30.0
_HOSTCTL_VERIFY_INTERVAL_S = 2.0


@tool(
    "self_deploy",
    "Commit, test, and redeploy the agent's own changes under /opt/homelab-agent - "
    "the only safe way to ship a self-edit. Use this instead of a raw `git commit` "
    "plus `systemctl restart homelab-agent` via guest_exec/host_exec: it stages and "
    "commits exactly agent/, hostctl/, tests/, and pyproject.toml with the message "
    "you give it, runs the full test suite (pytest, ruff, mypy --strict) before "
    "touching the running service, reverts and reports failure instead of "
    "restarting if any of that fails, best-effort pushes to origin so the owner can "
    "see and revert the change on GitHub (pushing needs a credential this checkout "
    "may not have - it says plainly if that happened), then restarts the service "
    "and verifies it actually came back before calling this a success. If the "
    "restarted service does not come back cleanly within the verification window, "
    "it rolls back to the previous commit, reinstalls, restarts, and reports that "
    "it rolled itself back - automatically, with no further tool call from you. "
    "Only call this after you've already edited the files (e.g. via guest_exec on "
    "guest 104) and are ready to ship - it stages and commits everything currently "
    "changed under those four paths, so make sure nothing unrelated is sitting "
    "there half-finished. `commit_message` should say what changed and why, in your "
    "own words - it becomes the git commit message.",
    {
        "type": "object",
        "properties": {"commit_message": {"type": "string", "minLength": 1}},
        "required": ["commit_message"],
        "additionalProperties": False,
    },
)
def self_deploy(commit_message: str) -> dict[str, Any]:
    if not _SELF_DEPLOY_SCRIPT.exists():
        raise RuntimeError(
            f"deploy/self-deploy.sh not found at {_SELF_DEPLOY_SCRIPT} - this checkout "
            "is missing the wrapper script this tool depends on"
        )
    msg_path = Path("/tmp") / f"self-deploy-msg-{int(time.time() * 1000)}.txt"
    msg_path.write_text(commit_message.strip() + "\n", encoding="utf-8")
    try:
        proc = subprocess.run(
            ["bash", str(_SELF_DEPLOY_SCRIPT), str(msg_path)],
            capture_output=True,
            text=True,
            timeout=_SELF_DEPLOY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        # The script itself has already committed and possibly restarted by
        # this point in the worst case - a timeout here means *this call*
        # gave up waiting, not that the script's own rollback logic didn't
        # run. It keeps running to completion regardless (see the module
        # docstring and agent/improve.py's notes on this).
        raw_partial = exc.stdout
        partial = (
            raw_partial.decode(errors="replace")
            if isinstance(raw_partial, bytes)
            else raw_partial
        )
        raise RuntimeError(
            f"self-deploy.sh did not finish within {_SELF_DEPLOY_TIMEOUT_S}s; it may "
            "still be running in the background and will finish its own "
            "test/restart/verify/rollback sequence on its own. stdout so far: "
            f"{(partial or '')[-2000:]}"
        ) from exc
    finally:
        msg_path.unlink(missing_ok=True)

    output = proc.stdout or ""
    result: dict[str, Any] | None = None
    for line in reversed(output.splitlines()):
        if line.startswith("RESULT_JSON: "):
            try:
                result = json.loads(line[len("RESULT_JSON: ") :])
            except json.JSONDecodeError:
                result = None
            break
    if result is None:
        raise RuntimeError(
            f"self-deploy.sh exited {proc.returncode} with no parseable result line. "
            f"stdout: {output[-2000:]!r} stderr: {(proc.stderr or '')[-1000:]!r}"
        )
    result["log_tail"] = output[-3000:]
    return result


@tool(
    "hostctl_restart_verified",
    "Restart hostctl on the Proxmox host and verify it actually came back before "
    "reporting success - the only safe way to restart it. hostctl is the agent's "
    "entire root-on-host boundary: guest_exec, host_exec, zfs_snapshot, and every "
    "other infra tool go through it, so a hostctl that doesn't come back strands "
    "the agent with no way to reach the host or any guest, including itself. Use "
    "this instead of `systemctl restart hostctl` via host_exec directly. There is "
    "no automatic code rollback for hostctl (it's deployed by rsync, not a git "
    "checkout this agent manages) - if it doesn't come back, this reports that "
    "plainly and clearly as a serious problem needing the owner's attention, "
    "rather than silently retrying or pretending it recovered. Only restart "
    "hostctl at all if you have a specific, concrete reason to believe it's "
    "actually necessary.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def hostctl_restart_verified() -> dict[str, Any]:
    try:
        hostctl_post("/host/exec", {"command": "systemctl restart hostctl"}, timeout=EXEC_TIMEOUT)
    except Exception as exc:
        # Expected in the common case: hostctl kills its own connection
        # mid-restart, so the response to the very request that triggered
        # the restart often never arrives. Move straight to polling rather
        # than treating this as a failure.
        logger.info("hostctl restart request did not complete cleanly (expected): %s", exc)

    deadline = time.monotonic() + _HOSTCTL_VERIFY_TIMEOUT_S
    attempts = 0
    while time.monotonic() < deadline:
        time.sleep(_HOSTCTL_VERIFY_INTERVAL_S)
        attempts += 1
        try:
            hostctl_get("/guests")
        except Exception:
            continue
        return {"restarted": True, "came_back": True, "attempts": attempts}
    return {
        "restarted": True,
        "came_back": False,
        "attempts": attempts,
        "warning": (
            "hostctl did not answer /guests within "
            f"{_HOSTCTL_VERIFY_TIMEOUT_S:.0f}s of being restarted. There is no "
            "automatic rollback available for it - this needs the owner's "
            "attention directly on the Proxmox host."
        ),
    }
