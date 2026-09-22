import time

import pytest
from fastapi.testclient import TestClient

from hostctl import jobs, pve
from hostctl.app import app

AUTH = {"Authorization": "Bearer testtoken"}


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HOSTCTL_TOKEN", "testtoken")
    monkeypatch.setenv("HOSTCTL_JOBS_DIR", str(tmp_path / "jobs"))


def _wait_until(predicate, *, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")


def test_post_jobs_requires_auth():
    r = TestClient(app).post("/jobs", json={"target": "host", "command": "echo hi"})
    assert r.status_code == 401


def test_post_jobs_starts_a_job_and_returns_201():
    r = TestClient(app).post(
        "/jobs", json={"target": "host", "command": "echo hi"}, headers=AUTH
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "running"
    assert body["target"] == "host"
    assert body["command"] == "echo hi"
    assert "job_id" in body and "started_at" in body


def test_get_job_by_id_reaches_terminal_status():
    client = TestClient(app)
    start = client.post("/jobs", json={"target": "host", "command": "exit 3"}, headers=AUTH)
    job_id = start.json()["job_id"]

    _wait_until(lambda: client.get(f"/jobs/{job_id}", headers=AUTH).json()["status"] != "running")
    r = client.get(f"/jobs/{job_id}", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["exitcode"] == 3


def test_get_unknown_job_is_404():
    r = TestClient(app).get("/jobs/j-doesnotexist", headers=AUTH)
    assert r.status_code == 404


def test_get_jobs_lists_started_jobs():
    client = TestClient(app)
    start = client.post("/jobs", json={"target": "host", "command": "echo hi"}, headers=AUTH)
    job_id = start.json()["job_id"]

    r = client.get("/jobs", headers=AUTH)
    assert r.status_code == 200
    ids = {j["job_id"] for j in r.json()["jobs"]}
    assert job_id in ids


def test_empty_command_is_403():
    r = TestClient(app).post("/jobs", json={"target": "host", "command": "   "}, headers=AUTH)
    assert r.status_code == 403


def test_malformed_target_is_422_from_request_validation():
    r = TestClient(app).post(
        "/jobs", json={"target": "not-a-target", "command": "echo hi"}, headers=AUTH
    )
    assert r.status_code == 422


def test_unknown_guest_target_is_422(monkeypatch):
    def _boom(guest_id: int) -> str:
        raise ValueError(f"unknown guest {guest_id}")

    monkeypatch.setattr(pve, "kind_of", _boom)
    r = TestClient(app).post(
        "/jobs", json={"target": "999", "command": "echo hi"}, headers=AUTH
    )
    assert r.status_code == 422


def test_qemu_agent_unavailable_is_503(monkeypatch):
    monkeypatch.setattr(pve, "kind_of", lambda guest_id: "qemu")

    def _boom_ping(argv, **kwargs):
        raise jobs.subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(jobs.subprocess, "run", _boom_ping)
    r = TestClient(app).post(
        "/jobs", json={"target": "200", "command": "echo hi"}, headers=AUTH
    )
    assert r.status_code == 503


def test_tail_bytes_query_param_is_respected():
    client = TestClient(app)
    start = client.post(
        "/jobs", json={"target": "host", "command": "printf '0123456789'"}, headers=AUTH
    )
    job_id = start.json()["job_id"]
    _wait_until(lambda: client.get(f"/jobs/{job_id}", headers=AUTH).json()["status"] != "running")

    r = client.get(f"/jobs/{job_id}", headers=AUTH, params={"tail_bytes": 4})
    assert r.json()["stdout_tail"] == "6789"
