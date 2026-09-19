import json

import pytest

from hostctl import pve

NODES_JSON = json.dumps([{"node": "tech"}])
LXC_JSON = json.dumps(
    [
        {"vmid": 101, "name": "docker", "status": "running"},
        {"vmid": 103, "name": "mail", "status": "running"},
    ]
)
QEMU_JSON = json.dumps([{"vmid": 200, "name": "mt5", "status": "running"}])


def test_raw_guests_invokes_pvesh_with_resolved_node(monkeypatch):
    """Regression test for the live-host defect: `pct list --output-format
    json` / `qm list --output-format json` do not exist on this Proxmox
    version. This test mocks only `_run` (the actual subprocess boundary),
    not `_raw_guests` itself, so the exact command shape is asserted rather
    than assumed."""
    seen: list[list[str]] = []

    def _fake_run(argv: list[str]) -> str:
        seen.append(argv)
        if argv == ["pvesh", "get", "/nodes", "--output-format", "json"]:
            return NODES_JSON
        if argv == ["pvesh", "get", "/nodes/tech/lxc", "--output-format", "json"]:
            return LXC_JSON
        if argv == ["pvesh", "get", "/nodes/tech/qemu", "--output-format", "json"]:
            return QEMU_JSON
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(pve, "_run", _fake_run)

    guests = pve._raw_guests()

    assert seen == [
        ["pvesh", "get", "/nodes", "--output-format", "json"],
        ["pvesh", "get", "/nodes/tech/lxc", "--output-format", "json"],
        ["pvesh", "get", "/nodes/tech/qemu", "--output-format", "json"],
    ]
    assert [g["id"] for g in guests] == [101, 103, 200]
    assert [g["kind"] for g in guests] == ["lxc", "lxc", "qemu"]


def test_node_name_raises_when_not_exactly_one_node(monkeypatch):
    monkeypatch.setattr(
        pve, "_run", lambda argv: json.dumps([{"node": "a"}, {"node": "b"}])
    )
    with pytest.raises(RuntimeError):
        pve._node_name()
