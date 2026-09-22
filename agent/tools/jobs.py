"""Long-running commands as jobs the model can start and poll.

`guest_exec` and `host_exec` block on one HTTP response, so they inherit
hostctl's 300-second subprocess timeout. `docker compose pull` on a large
image routinely runs longer than that, and the synchronous path then
returns a timeout error while the pull is still going on the host. The
model reads that as a failure, says so in #homelab, and may retry work
that is already in flight.

A job fixes the category error rather than raising the timeout. hostctl
starts the command detached and hands back an id; the agent polls that id
and gets one of a small set of states back - running, succeeded, failed,
timed_out. "Still running" stops being an error and becomes a fact the
model can wait on.

Nothing can be stranded invisibly: `job_start` always returns an id before
anything else can go wrong, `job_status` answers for any id, and
`job_list` enumerates every job hostctl still remembers - so even a job
whose id got lost in a long conversation is one call away.

The hostctl side of this contract is owned by another agent; see the task
report for its exact specification. Everything here is the agent side.
"""

import re
from typing import Any

from agent import clients
from agent.tools.base import tool
from agent.tools.infra import NO_ARGS, _capped

# "host", or a guest id. Same spelling the exec tools use for `guest`, so
# there is one way to name a target across the whole tool surface.
TARGET_PATTERN = r"^(host|[0-9]{1,6})$"
_TARGET_RE = re.compile(TARGET_PATTERN)

JOB_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
_JOB_ID_RE = re.compile(JOB_ID_PATTERN)

# Job states hostctl reports. Anything else is passed through untouched
# rather than being mapped onto one of these - a state this agent doesn't
# recognise must not be quietly reported as success or failure.
RUNNING = "running"
TERMINAL = ("succeeded", "failed", "timed_out")


def _shape(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize one hostctl job record for the model.

    `still_running` is stated explicitly rather than left for the model to
    infer from `status`: the entire point of this tool is that "not
    finished yet" is not a failure, and that distinction should not depend
    on the model reading a status string correctly under pressure.
    """
    status = str(raw.get("status", "unknown"))
    stdout, stdout_cut, stdout_total = _capped(str(raw.get("stdout_tail") or ""))
    stderr, stderr_cut, stderr_total = _capped(str(raw.get("stderr_tail") or ""))
    return {
        "job_id": raw.get("job_id"),
        "target": raw.get("target"),
        "command": raw.get("command"),
        "status": status,
        "still_running": status == RUNNING,
        "finished": status in TERMINAL,
        "exitcode": raw.get("exitcode"),
        "started_at": raw.get("started_at"),
        "finished_at": raw.get("finished_at"),
        "runtime_s": raw.get("runtime_s"),
        "stdout_tail": stdout,
        "stdout_truncated": stdout_cut,
        "stdout_total_chars": stdout_total,
        "stderr_tail": stderr,
        "stderr_truncated": stderr_cut,
        "stderr_total_chars": stderr_total,
    }


def _require_target(target: str) -> str:
    if not _TARGET_RE.match(target):
        raise ValueError(f"invalid target: {target!r} (use 'host' or a numeric guest id)")
    return target


def _require_job_id(job_id: str) -> str:
    if not _JOB_ID_RE.match(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    return job_id


@tool(
    "job_start",
    "Start a shell command that may run longer than a few minutes, detached on the "
    "host or inside a guest, and get back a job id immediately instead of waiting for "
    "it to finish. Use this INSTEAD of guest_exec/host_exec for anything that can "
    "exceed ~5 minutes: `docker compose pull` on a large image, a big copy or rsync, a "
    "long build, a database dump. The command keeps running on the host whether or not "
    "you poll it. Poll with job_status(job_id) - a job that is still running is not a "
    "failure, and reporting it as one is wrong. `target` is 'host' for the Proxmox "
    "host itself or a numeric guest id (e.g. '101') for a guest; `command` is a shell "
    "one-liner in that target's native syntax, exactly as for host_exec/guest_exec.",
    {
        "type": "object",
        "properties": {
            "target": {"type": "string", "pattern": TARGET_PATTERN},
            "command": {"type": "string", "minLength": 1},
        },
        "required": ["target", "command"],
        "additionalProperties": False,
    },
)
def job_start(target: str, command: str) -> dict[str, Any]:
    raw = clients.hostctl_job_start(_require_target(target), command)
    shaped = _shape({"target": target, "command": command, **raw})
    shaped["note"] = (
        "The command is running on the host now. Poll job_status with this job_id; "
        "status 'running' means it has not finished, which is not a failure."
    )
    return shaped


@tool(
    "job_status",
    "Check on a job started with job_start, by its id. Returns its status - running, "
    "succeeded, failed, or timed_out - along with the exit code once it has one and "
    "the output collected so far (capped). 'running' means exactly that: the command "
    "is still going, so wait and poll again rather than treating it as an error or "
    "starting the same work a second time.",
    {
        "type": "object",
        "properties": {"job_id": {"type": "string", "pattern": JOB_ID_PATTERN}},
        "required": ["job_id"],
        "additionalProperties": False,
    },
)
def job_status(job_id: str) -> dict[str, Any]:
    return _shape(clients.hostctl_job_status(_require_job_id(job_id)))


@tool(
    "job_list",
    "List every long-running job hostctl still remembers, running ones included. Use "
    "this when you have lost track of a job id, or to check before starting work that "
    "may already be in flight - an image pull started by an earlier tick is still "
    "running here even though it is not in this conversation.",
    NO_ARGS,
)
def job_list() -> dict[str, Any]:
    raw = clients.hostctl_job_list()
    jobs = raw.get("jobs")
    if not isinstance(jobs, list):
        return {"jobs": []}
    return {"jobs": [_shape(job) for job in jobs if isinstance(job, dict)]}
