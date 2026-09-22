"""Detached, pollable jobs - the fix for `guest_exec`/`host_exec` blocking
on one HTTP response and inheriting hostctl's 300s subprocess timeout.
`docker compose pull` on a large image routinely runs longer than that; the
synchronous path used to report a timeout for a pull that was still
downloading, which the model read as a failure and sometimes acted on by
retrying work already in flight.

The contract here (`/jobs`, `/jobs/{job_id}`) is owned by the agent side -
`agent/tools/jobs.py` and `agent/clients.py` - and is implemented exactly
as specified there, not improved on. In particular: the status vocabulary
is exactly `running`/`succeeded`/`failed`/`timed_out`, and `job_id` matches
`^[A-Za-z0-9_-]{1,64}$`.

Four things this module had to decide, each documented at the point it
matters below:

1. **Survive a `hostctl` restart.** Every job is a directory under
   `JOBS_DIR` (`meta.json` + `stdout.log`/`stderr.log` + `exit.code`), not
   an in-memory dict - `hostctl-deploy.sh` restarts this process as part of
   an ordinary deploy, and an in-flight pull must not become unknowable
   just because that happened.

2. **Output capture without unbounded growth in this process.** The
   spawned shell redirects its own stdout/stderr straight to files on
   disk (`>stdout.log 2>stderr.log`); hostctl never holds job output in
   memory, and a tail read is a bounded read from the end of a file.

3. **Reaping.** `_reap()` runs on every `start_job`/`list_jobs` call and
   deletes terminal job directories once they're older than
   `JOB_RETENTION_S` (24h, matching `agent/tools/outbox.py`'s own
   retention window) or once there are more than `JOB_RETENTION_COUNT`
   terminal jobs, oldest first - the same two-pronged cap
   `hostctl/zfs.py`'s snapshot rate limit + count cap already uses for the
   same reason (bound both the rate and the total, not just one).

4. **Orphans.** The spawned process tree is fully detached
   (`start_new_session=True`) specifically so it keeps running if hostctl
   itself dies mid-job - and the exit code is captured by the spawned
   shell writing it to `exit.code` itself as its last action, not by
   hostctl calling `wait()`, because a *restarted* hostctl process is not
   the parent of a job's child process and cannot `wait()` it for a real
   exit code at all. If `exit.code` never appears and the tracked pid is
   also gone (the process died some other way - host reboot, OOM kill),
   there is no way to recover a real exit code, and this module never
   fabricates a success for a job it cannot verify: it is recorded as
   `failed` with `exitcode=-1` and a note appended to `stderr.log`. That
   picks a real value from the agent's fixed vocabulary rather than
   inventing an "unknown" status the agent has no code path for - see its
   contract, which explicitly does not want new status values.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import shlex
import subprocess
from pathlib import Path
from typing import Any

from hostctl import pve

DEFAULT_JOBS_DIR = "/var/lib/hostctl/jobs"
DEFAULT_TIMEOUT_S = 3600  # an hour - reasonable for an image pull
DEFAULT_TAIL_BYTES = 4096  # "a few KB"
JOB_RETENTION_S = 24 * 3600
JOB_RETENTION_COUNT = 200
QEMU_AGENT_PING_TIMEOUT_S = 10

STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"

ORPHAN_EXITCODE = -1
_ORPHAN_NOTE = (
    "\n[hostctl] this job's process could no longer be found and it never recorded "
    "an exit code - hostctl likely restarted or the process was killed by something "
    "outside its control (host reboot, OOM). Reported as failed; the real outcome "
    "is unknown.\n"
)
TIMEOUT_EXITCODE = 124  # GNU coreutils `timeout`'s own convention


class TargetRefusedError(PermissionError):
    """Mirrors `guest_shell`/`host_shell` raising `PermissionError` for an
    empty command - the existing exec routes map that to 403 via
    `_exec_error_to_http`, and job routes reuse the same mapping rather
    than inventing a second meaning for "refused"."""


def _jobs_dir() -> Path:
    return Path(os.environ.get("HOSTCTL_JOBS_DIR", DEFAULT_JOBS_DIR))


def _job_dir(job_id: str) -> Path:
    return _jobs_dir() / job_id


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(t: dt.datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _new_job_id(existing: set[str]) -> str:
    for _ in range(10):
        job_id = f"j-{secrets.token_hex(3)}"
        if job_id not in existing:
            return job_id
    raise RuntimeError("could not allocate a unique job id")


def _reap_zombie(pid: int) -> None:
    """Best-effort, non-blocking `wait()` on a job's pid.

    hostctl is the direct parent of every job it starts (`start_new_session`
    detaches from the controlling terminal/session, not from the
    parent-child relationship the kernel tracks) - so as long as hostctl
    hasn't restarted since a job finished, a job that already wrote
    `exit.code` and exited is sitting in the process table as a zombie
    until something calls `wait()` on it, and `kill(pid, 0)` reports a
    zombie as very much alive. Without this, `_pid_alive` would never see
    a finished job's pid go away while hostctl keeps running, since
    nothing else in this module calls `wait()`. Silently does nothing if
    hostctl is no longer that pid's parent (a restart, or it was already
    reaped) - `waitpid` raising `ChildProcessError` in either case.
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def _pid_alive(pid: int) -> bool:
    _reap_zombie(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, just owned by someone else - hostctl runs as root, so in
        # practice this shouldn't happen, but "exists" is still the
        # correct read of a PermissionError from kill(2).
        return True
    return True


def _build_script(target: str, command: str, timeout_s: int, stdout: Path, stderr: Path) -> str:
    """The one shell script a job actually runs, detached. Dispatches
    exactly as `guest_shell`/`host_shell` do (`sh -c` on the host and any
    LXC, `cmd.exe /c` on a QEMU guest), wrapped so that (a) output is
    captured straight to files and (b) the exit code is written to
    `exit.code` by the script itself, not read back by hostctl via
    `wait()` - see the module docstring point 4 for why that distinction
    is the whole reason a restart doesn't strand a job.
    """
    stdout_q, stderr_q = shlex.quote(str(stdout)), shlex.quote(str(stderr))
    exit_path = stdout.parent / "exit.code"
    exit_tmp = stdout.parent / "exit.code.tmp"
    exit_q, exit_tmp_q = shlex.quote(str(exit_path)), shlex.quote(str(exit_tmp))

    if target == "host":
        inner = f"timeout {timeout_s}s sh -c {shlex.quote(command)}"
    else:
        guest_id = int(target)
        kind = pve.kind_of(guest_id)
        if kind == "lxc":
            inner = f"timeout {timeout_s}s pct exec {guest_id} -- sh -c {shlex.quote(command)}"
        else:
            # `qm guest exec` blocks for up to its own --timeout and only
            # prints its JSON result envelope once the guest-side command
            # finishes - it does not stream, so stdout_tail/stderr_tail
            # for a QEMU-guest job will not grow mid-run the way an
            # LXC/host job's does. That's an inherent limit of the QEMU
            # guest agent exec primitive, not a shortcut taken here. The
            # outer `timeout` is a backstop in case `qm`'s own --timeout
            # is ever not honoured; it cannot guarantee the guest-side
            # process actually stops (killing the local `qm` client does
            # not necessarily cancel work already dispatched to the guest
            # agent).
            inner = (
                f"timeout {timeout_s + 30}s qm guest exec {guest_id} --timeout {timeout_s} "
                f"-- cmd.exe /c {shlex.quote(command)}"
            )

    return (
        f"{{ {inner} ; }} >{stdout_q} 2>{stderr_q}; "
        f'code=$?; printf \'%s\' "$code" >{exit_tmp_q}; mv {exit_tmp_q} {exit_q}'
    )


def _check_target_startable(target: str) -> None:
    """Everything that can be known *before* spawning: does the target
    exist, and (for a QEMU guest) does its guest agent even answer. Maps
    onto the same error vocabulary the existing exec routes use -
    `ValueError` (unknown guest) -> 422 "could not be started at all",
    `pve.GuestAgentUnavailableError` -> 503 - via `_exec_error_to_http` in
    `hostctl/app.py`, unchanged.
    """
    if target == "host":
        return
    guest_id = int(target)
    kind = pve.kind_of(guest_id)  # raises ValueError for an unknown guest
    if kind != "qemu":
        return
    try:
        subprocess.run(
            ["qm", "agent", str(guest_id), "ping"],
            capture_output=True,
            text=True,
            check=True,
            timeout=QEMU_AGENT_PING_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise pve.GuestAgentUnavailableError(
            f"guest {guest_id}'s QEMU guest agent isn't responding - it may still be "
            "booting or the agent service may be stopped inside the guest"
        ) from exc


def start_job(target: str, command: str, timeout_s: int | None) -> dict[str, Any]:
    if not command.strip():
        raise TargetRefusedError("no command given")
    _check_target_startable(target)

    jobs_dir = _jobs_dir()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = _new_job_id({p.name for p in jobs_dir.iterdir() if p.is_dir()})
    job_dir = _job_dir(job_id)
    job_dir.mkdir()

    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"
    stdout_path.touch()
    stderr_path.touch()
    effective_timeout = timeout_s if timeout_s and timeout_s > 0 else DEFAULT_TIMEOUT_S

    # `_check_target_startable` above already resolved `target` via
    # `pve.kind_of`, so this can't raise ValueError for an unknown guest at
    # this point - it's called again here only to pick the right dispatch
    # shape, not to re-validate.
    script = _build_script(target, command, effective_timeout, stdout_path, stderr_path)

    started_at = _now()
    try:
        proc = subprocess.Popen(
            ["sh", "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # survives hostctl exiting/restarting
        )
    except OSError as exc:
        job_dir_cleanup(job_dir)
        raise RuntimeError(f"could not start job: {exc}") from exc

    meta = {
        "job_id": job_id,
        "target": target,
        "command": command,
        "timeout_s": effective_timeout,
        "pid": proc.pid,
        "started_at": _iso(started_at),
    }
    (job_dir / "meta.json").write_text(json.dumps(meta))

    _reap()

    return {
        "job_id": job_id,
        "target": target,
        "command": command,
        "status": STATUS_RUNNING,
        "started_at": _iso(started_at),
    }


def job_dir_cleanup(job_dir: Path) -> None:
    for child in job_dir.iterdir():
        child.unlink()
    job_dir.rmdir()


def _tail(path: Path, tail_bytes: int) -> tuple[str, int]:
    total = path.stat().st_size if path.exists() else 0
    if not path.exists() or total == 0:
        return "", 0
    with path.open("rb") as f:
        if total > tail_bytes:
            f.seek(total - tail_bytes)
        data = f.read()
    return data.decode("utf-8", errors="replace"), total


def _resolve_status(job_dir: Path, meta: dict[str, Any]) -> tuple[str, int | None, str | None]:
    """Returns (status, exitcode, finished_at_iso)."""
    exit_path = job_dir / "exit.code"
    if exit_path.exists():
        # The process has already written its own exit code and exited by
        # this point, but hostctl (if it's still the parent - see
        # `_reap_zombie`) hasn't necessarily `wait()`-ed on it yet. Without
        # this, a job that completes normally would leak a zombie forever,
        # since nothing else on this path ever calls `_pid_alive`.
        pid = meta.get("pid")
        if isinstance(pid, int):
            _reap_zombie(pid)
        raw = exit_path.read_text().strip()
        code = int(raw) if raw else ORPHAN_EXITCODE
        finished_at = dt.datetime.fromtimestamp(exit_path.stat().st_mtime, tz=dt.UTC)
        if code == TIMEOUT_EXITCODE:
            status = STATUS_TIMED_OUT
        elif code == 0:
            status = STATUS_SUCCEEDED
        else:
            status = STATUS_FAILED
        return status, code, _iso(finished_at)

    pid = meta.get("pid")
    if isinstance(pid, int) and _pid_alive(pid):
        return STATUS_RUNNING, None, None

    # Orphaned: the process is gone but never wrote exit.code. Persist the
    # decision (see module docstring point 4) so a later poll doesn't
    # re-derive it from a possibly-reused pid, and note it once in
    # stderr.log so the tail explains the exitcode.
    exit_path.write_text(str(ORPHAN_EXITCODE))
    with (job_dir / "stderr.log").open("a") as f:
        f.write(_ORPHAN_NOTE)
    finished_at = _now()
    return STATUS_FAILED, ORPHAN_EXITCODE, _iso(finished_at)


def _read_job(job_dir: Path, *, tail_bytes: int) -> dict[str, Any]:
    meta = json.loads((job_dir / "meta.json").read_text())
    status, exitcode, finished_at = _resolve_status(job_dir, meta)

    stdout_tail, stdout_total = _tail(job_dir / "stdout.log", tail_bytes)
    stderr_tail, stderr_total = _tail(job_dir / "stderr.log", tail_bytes)

    started_at = dt.datetime.fromisoformat(meta["started_at"].replace("Z", "+00:00"))
    if finished_at is not None:
        finished_dt = dt.datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        runtime_s: float | None = round((finished_dt - started_at).total_seconds(), 3)
    else:
        runtime_s = round((_now() - started_at).total_seconds(), 3)

    return {
        "job_id": meta["job_id"],
        "target": meta["target"],
        "command": meta["command"],
        "status": status,
        "exitcode": exitcode,
        "started_at": meta["started_at"],
        "finished_at": finished_at,
        "runtime_s": runtime_s,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "stdout_total_bytes": stdout_total,
        "stderr_total_bytes": stderr_total,
    }


def get_job(job_id: str, *, tail_bytes: int = DEFAULT_TAIL_BYTES) -> dict[str, Any]:
    job_dir = _job_dir(job_id)
    if not job_dir.is_dir() or not (job_dir / "meta.json").exists():
        raise KeyError(job_id)
    return _read_job(job_dir, tail_bytes=tail_bytes)


def list_jobs(*, tail_bytes: int = DEFAULT_TAIL_BYTES) -> list[dict[str, Any]]:
    _reap()
    jobs_dir = _jobs_dir()
    if not jobs_dir.is_dir():
        return []
    jobs = []
    for job_dir in jobs_dir.iterdir():
        if not job_dir.is_dir() or not (job_dir / "meta.json").exists():
            continue
        jobs.append(_read_job(job_dir, tail_bytes=tail_bytes))
    jobs.sort(key=lambda j: j["started_at"], reverse=True)
    return jobs


def _reap() -> None:
    """Delete terminal job directories once they're old, or once there are
    too many of them - see module docstring point 3. Running jobs are
    never touched here regardless of age."""
    jobs_dir = _jobs_dir()
    if not jobs_dir.is_dir():
        return

    terminal: list[tuple[Path, dt.datetime]] = []
    for job_dir in jobs_dir.iterdir():
        if not job_dir.is_dir() or not (job_dir / "meta.json").exists():
            continue
        try:
            meta = json.loads((job_dir / "meta.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        status, _exitcode, finished_at = _resolve_status(job_dir, meta)
        if status == STATUS_RUNNING or finished_at is None:
            continue
        finished_dt = dt.datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        terminal.append((job_dir, finished_dt))

    now = _now()
    terminal.sort(key=lambda pair: pair[1])  # oldest first
    stale_by_age = [
        d for d, finished in terminal if (now - finished).total_seconds() > JOB_RETENTION_S
    ]
    excess_count = len(terminal) - JOB_RETENTION_COUNT
    stale_by_count = [d for d, _f in terminal[: max(excess_count, 0)]]

    for job_dir in {*stale_by_age, *stale_by_count}:
        job_dir_cleanup(job_dir)
