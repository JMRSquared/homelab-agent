import pytest
from fastapi.testclient import TestClient

from hostctl import pve
from hostctl.app import app

AUTH = {"Authorization": "Bearer testtoken"}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("HOSTCTL_TOKEN", "testtoken")


def test_list_guests_strips_vm_200(monkeypatch):
    monkeypatch.setattr(
        pve,
        "_raw_guests",
        lambda: [
            {"id": 101, "name": "docker", "kind": "lxc", "status": "running"},
            {"id": 200, "name": "mt5", "kind": "qemu", "status": "running"},
        ],
    )
    body = TestClient(app).get("/guests", headers=AUTH).json()
    assert [g["id"] for g in body["guests"]] == [101]


def test_missing_token_is_401():
    assert TestClient(app).get("/guests").status_code == 401


def test_requests_are_logged_with_body_and_status_without_authorization(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(pve, "_raw_guests", lambda: [])
    with caplog.at_level(logging.INFO, logger="hostctl.access"):
        TestClient(app).get("/guests", headers=AUTH)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "GET" in message
    assert "/guests" in message
    assert "status=200" in message
    assert "testtoken" not in message
    assert "Bearer" not in message


def test_failed_requests_are_logged_too(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="hostctl.access"):
        TestClient(app).get("/guests")  # no auth header -> 401
    assert len(caplog.records) == 1
    assert "status=401" in caplog.records[0].getMessage()


def test_token_file_is_read_when_set(tmp_path, monkeypatch):
    monkeypatch.setattr(pve, "_raw_guests", lambda: [])
    token_file = tmp_path / "token"
    token_file.write_text("filetoken\n")
    monkeypatch.delenv("HOSTCTL_TOKEN", raising=False)
    monkeypatch.setenv("HOSTCTL_TOKEN_FILE", str(token_file))
    r = TestClient(app).get("/guests", headers={"Authorization": "Bearer filetoken"})
    assert r.status_code == 200
