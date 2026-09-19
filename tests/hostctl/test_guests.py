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
