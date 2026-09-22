import json
import os
import signal
import time

import pytest

from hostctl import jobs, pve


@pytest.fixture(autouse=True)
def _jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOSTCTL_JOBS_DIR", str(tmp_path / "jobs"))
    return tmp_path / "jobs"


def _wait_until(predicate, *, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")


def test_start_returns_immediately_with_running_status():
    started = time.monotonic()
    result = jobs.start_job("host", "sleep 2", None)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0  # did not block for the sleep
    assert result["status"] == "running"
    assert result["target"] == "host"
    assert result["command"] == "sleep 2"
    assert "started_at" in result
    import re

    assert re.match(r"^[A-Za-z0-9_-]{1,64}$", result["job_id"])


def test_poll_shows_running_then_succeeded():
    result = jobs.start_job("host", "sleep 0.3; echo done", None)
    job_id = result["job_id"]

    first = jobs.get_job(job_id)
    assert first["status"] == "running"
    assert first["exitcode"] is None
    assert first["finished_at"] is None

    _wait_until(lambda: jobs.get_job(job_id)["status"] != "running", timeout=5)
    final = jobs.get_job(job_id)
    assert final["status"] == "succeeded"
    assert final["exitcode"] == 0
    assert final["finished_at"] is not None
    assert "done" in final["stdout_tail"]


def test_failed_command_reports_failed_with_exit_code():
    result = jobs.start_job("host", "exit 7", None)
    _wait_until(lambda: jobs.get_job(result["job_id"])["status"] != "running")
    final = jobs.get_job(result["job_id"])
    assert final["status"] == "failed"
    assert final["exitcode"] == 7


def test_timeout_reports_timed_out():
    result = jobs.start_job("host", "sleep 5", 1)
    _wait_until(lambda: jobs.get_job(result["job_id"])["status"] != "running", timeout=5)
    final = jobs.get_job(result["job_id"])
    assert final["status"] == "timed_out"
    assert final["exitcode"] == 124


def test_tails_grow_across_polls():
    result = jobs.start_job(
        "host", "echo one; sleep 0.4; echo two; sleep 0.4; echo three", None
    )
    job_id = result["job_id"]

    time.sleep(0.15)
    first = jobs.get_job(job_id)
    _wait_until(lambda: jobs.get_job(job_id)["stdout_total_bytes"] > first["stdout_total_bytes"])
    second = jobs.get_job(job_id)
    assert second["stdout_total_bytes"] > first["stdout_total_bytes"]
    assert second["stdout_tail"].startswith(first["stdout_tail"])

    _wait_until(lambda: jobs.get_job(job_id)["status"] != "running", timeout=5)
    final = jobs.get_job(job_id)
    assert "one" in final["stdout_tail"]
    assert "two" in final["stdout_tail"]
    assert "three" in final["stdout_tail"]


def test_tail_bytes_caps_what_is_returned():
    result = jobs.start_job("host", "printf '0123456789'", None)
    _wait_until(lambda: jobs.get_job(result["job_id"])["status"] != "running")
    capped = jobs.get_job(result["job_id"], tail_bytes=4)
    assert capped["stdout_tail"] == "6789"
    assert capped["stdout_total_bytes"] == 10


def test_restart_does_not_lose_a_running_job(tmp_path, monkeypatch):
    """A fresh `get_job`/`list_jobs` call reads everything from disk with no
    in-memory job registry at all, so there is nothing for a `hostctl`
    restart to lose - this pins that down by reading a job directory this
    test writes by hand, standing in for "a previous hostctl process
    started this and then got restarted"."""
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setenv("HOSTCTL_JOBS_DIR", str(jobs_dir))
    job_dir = jobs_dir / "j-abc123"
    job_dir.mkdir(parents=True)
    (job_dir / "stdout.log").write_text("partial output\n")
    (job_dir / "stderr.log").write_text("")
    # A real long-lived process to stand in for the job's still-running pid.
    import subprocess

    proc = subprocess.Popen(["sleep", "5"])
    try:
        (job_dir / "meta.json").write_text(
            json.dumps(
                {
                    "job_id": "j-abc123",
                    "target": "host",
                    "command": "docker compose pull",
                    "timeout_s": 3600,
                    "pid": proc.pid,
                    "started_at": "2026-01-01T00:00:00Z",
                }
            )
        )
        result = jobs.get_job("j-abc123")
        assert result["status"] == "running"
        assert result["stdout_tail"] == "partial output\n"
        assert result["job_id"] == "j-abc123"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_orphaned_job_is_reported_as_failed_not_a_new_status(tmp_path, monkeypatch):
    """If the tracked pid is gone and exit.code never appeared (the process
    was killed some way that skipped the wrapper's own trailer, e.g.
    SIGKILL), this must resolve to one of the agent's four known statuses -
    never an invented one - and never silently report success it cannot
    verify."""
    result = jobs.start_job("host", "sleep 30", None)
    job_id = result["job_id"]
    meta = json.loads(
        (jobs._job_dir(job_id) / "meta.json").read_text()  # type: ignore[attr-defined]
    )
    os.kill(meta["pid"], signal.SIGKILL)
    _wait_until(lambda: not jobs._pid_alive(meta["pid"]))

    final = jobs.get_job(job_id)
    assert final["status"] == "failed"
    assert final["exitcode"] == jobs.ORPHAN_EXITCODE
    assert "hostctl" in final["stderr_tail"]

    # And it stays that way on a second read (persisted, not re-derived
    # from a possibly-reused pid each time).
    again = jobs.get_job(job_id)
    assert again["status"] == "failed"
    assert again["exitcode"] == jobs.ORPHAN_EXITCODE


def test_empty_command_is_refused():
    with pytest.raises(PermissionError):
        jobs.start_job("host", "   ", None)


def test_unknown_guest_target_cannot_be_started(monkeypatch):
    def _boom(guest_id: int) -> str:
        raise ValueError(f"unknown guest {guest_id}")

    monkeypatch.setattr(pve, "kind_of", _boom)
    with pytest.raises(ValueError):
        jobs.start_job("999", "echo hi", None)


def test_qemu_guest_with_unavailable_agent_raises(monkeypatch):
    monkeypatch.setattr(pve, "kind_of", lambda guest_id: "qemu")

    def _boom_ping(argv, **kwargs):
        raise jobs.subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(jobs.subprocess, "run", _boom_ping)
    with pytest.raises(pve.GuestAgentUnavailableError):
        jobs.start_job("200", "echo hi", None)


def test_lxc_guest_dispatches_via_pct(monkeypatch):
    monkeypatch.setattr(pve, "kind_of", lambda guest_id: "lxc")
    stdout_path = jobs._job_dir("x") / "stdout.log"
    stderr_path = jobs._job_dir("x") / "stderr.log"
    script = jobs._build_script("101", "docker compose pull", 60, stdout_path, stderr_path)
    assert "pct exec 101 -- sh -c" in script


def test_reap_deletes_old_terminal_jobs_by_age(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setenv("HOSTCTL_JOBS_DIR", str(jobs_dir))
    old_dir = jobs_dir / "j-old000"
    old_dir.mkdir(parents=True)
    (old_dir / "stdout.log").write_text("")
    (old_dir / "stderr.log").write_text("")
    (old_dir / "meta.json").write_text(
        json.dumps(
            {
                "job_id": "j-old000",
                "target": "host",
                "command": "echo old",
                "timeout_s": 60,
                "pid": 1,  # pid 1 (init) always exists, so this reads as "not running"
                "started_at": "2020-01-01T00:00:00Z",
            }
        )
    )
    (old_dir / "exit.code").write_text("0")
    old_time = time.time() - jobs.JOB_RETENTION_S - 3600
    os.utime(old_dir / "exit.code", (old_time, old_time))

    jobs.list_jobs()

    assert not old_dir.exists()


def test_reap_caps_total_terminal_jobs(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setenv("HOSTCTL_JOBS_DIR", str(jobs_dir))
    monkeypatch.setattr(jobs, "JOB_RETENTION_COUNT", 3)

    for i in range(6):
        d = jobs_dir / f"j-{i:06d}"
        d.mkdir(parents=True)
        (d / "stdout.log").write_text("")
        (d / "stderr.log").write_text("")
        (d / "meta.json").write_text(
            json.dumps(
                {
                    "job_id": f"j-{i:06d}",
                    "target": "host",
                    "command": "echo x",
                    "timeout_s": 60,
                    "pid": 1,
                    "started_at": "2026-01-01T00:00:00Z",
                }
            )
        )
        (d / "exit.code").write_text("0")
        t = time.time() - (6 - i)  # later index = more recently finished
        os.utime(d / "exit.code", (t, t))

    remaining = jobs.list_jobs()
    assert len(remaining) == 3
    # The three most recently finished survive.
    assert {j["job_id"] for j in remaining} == {"j-000003", "j-000004", "j-000005"}
