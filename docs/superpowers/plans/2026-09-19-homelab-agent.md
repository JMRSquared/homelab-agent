# Homelab Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An always-running agent on the Proxmox node `tech` that autonomously manages the homelab and serves Tech's family as an AI assistant through Slack.

**Architecture:** A privileged `hostctl` HTTP service on the Proxmox host exposes a finite allowlist of host operations. A single long-running Python process in a new LXC 103 holds the Slack Socket Mode connection, a 60-second scheduler, a tool dispatcher, and a MiniMax M3 client. The agent has full autonomy over every verb `hostctl` and the service APIs expose; irreversible operations are absent from that surface rather than gated by approval.

**Tech Stack:** Python 3.11, FastAPI, uvicorn, slack-bolt (Socket Mode), APScheduler, httpx, openai SDK pointed at MiniMax, caldav, pytest, respx.

**Spec:** `docs/homelab-agent-spec.md`

## Global Constraints

- Proxmox host is `root@10.0.0.2`. SSH from Tech's Mac uses key auth. Always pass `-n` when scripting: `ssh -n -o BatchMode=yes root@10.0.0.2 '<command>'`.
- **VM 200 (`mt5`) must never be reachable by the agent.** No route, tool, or config may name it. `hostctl` returns 403 for guest id 200 before routing.
- **Never place, modify, or close an MT5 trade**, including on the demo account.
- `zfs destroy`, `pct destroy`, `qm destroy`, and arbitrary host shell are not implemented anywhere in this codebase.
- Address physical disks by `/dev/disk/by-id/`, never `/dev/sdX`.
- Nested quoting through `ssh -> pct/qm -> shell` mangles. Write the file locally, `scp` to the host, then `pct push` it in.
- Python 3.11 minimum. Type hints on every public function. `ruff` and `mypy --strict` must pass.
- Secrets live in `/etc/homelab-agent/env` (LXC 103) and `/etc/hostctl/token` (host), mode `0600`, never in the repo.
- MiniMax base URL: `https://api.minimax.io/v1`. Model id: `MiniMax-M3`.
- Repo lives at `~/CODE/homelab-agent` on Tech's Mac, deployed by `make deploy`.

---

### Task 1: `hostctl` skeleton with guest listing and the VM 200 boundary

**Files:**
- Create: `hostctl/app.py`
- Create: `hostctl/auth.py`
- Create: `hostctl/pve.py`
- Create: `tests/hostctl/test_guests.py`
- Create: `pyproject.toml`

**Interfaces:**
- Consumes: nothing.
- Produces: `hostctl.pve.list_guests() -> list[Guest]` where `Guest` is a `TypedDict` with keys `id: int`, `name: str`, `kind: Literal["lxc","qemu"]`, `status: str`. `hostctl.auth.require_token(authorization: str | None) -> None` raising `HTTPException(401)`. FastAPI app object `hostctl.app.app`. Blocked guest ids are the module constant `hostctl.pve.BLOCKED_GUEST_IDS: frozenset[int]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/hostctl/test_guests.py
import pytest
from fastapi.testclient import TestClient
from hostctl.app import app
from hostctl import pve

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/hostctl/test_guests.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hostctl'`

- [ ] **Step 3: Write minimal implementation**

```python
# hostctl/auth.py
import hmac
import os
from fastapi import HTTPException


def require_token(authorization: str | None) -> None:
    expected = os.environ.get("HOSTCTL_TOKEN", "")
    prefix = "Bearer "
    if not expected or not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="unauthorized")
    if not hmac.compare_digest(authorization[len(prefix):], expected):
        raise HTTPException(status_code=401, detail="unauthorized")
```

```python
# hostctl/pve.py
import json
import subprocess
from typing import Literal, TypedDict

BLOCKED_GUEST_IDS: frozenset[int] = frozenset({200})


class Guest(TypedDict):
    id: int
    name: str
    kind: Literal["lxc", "qemu"]
    status: str


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=30).stdout


def _raw_guests() -> list[Guest]:
    out: list[Guest] = []
    for kind, cmd in (("lxc", "pct"), ("qemu", "qm")):
        rows = json.loads(_run([cmd, "list", "--output-format", "json"]))
        for row in rows:
            out.append(
                Guest(
                    id=int(row["vmid"]),
                    name=str(row.get("name") or row.get("hostname") or ""),
                    kind=kind,  # type: ignore[typeddict-item]
                    status=str(row["status"]),
                )
            )
    return out


def list_guests() -> list[Guest]:
    return [g for g in _raw_guests() if g["id"] not in BLOCKED_GUEST_IDS]
```

```python
# hostctl/app.py
from fastapi import Depends, FastAPI, Header
from hostctl.auth import require_token
from hostctl.pve import list_guests

app = FastAPI(title="hostctl")


def _auth(authorization: str | None = Header(default=None)) -> None:
    require_token(authorization)


@app.get("/guests", dependencies=[Depends(_auth)])
def guests() -> dict[str, object]:
    return {"guests": list_guests()}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/hostctl/test_guests.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add hostctl tests/hostctl pyproject.toml
git commit -m "feat(hostctl): guest listing with VM 200 excluded from the surface"
```

---

### Task 2: `hostctl` actions, ZFS, disks and host metrics

**Files:**
- Modify: `hostctl/app.py`
- Modify: `hostctl/pve.py`
- Create: `hostctl/zfs.py`
- Create: `hostctl/metrics.py`
- Create: `tests/hostctl/test_actions.py`
- Create: `tests/hostctl/test_zfs.py`

**Interfaces:**
- Consumes: `hostctl.pve.BLOCKED_GUEST_IDS`, `hostctl.auth.require_token`.
- Produces: `pve.guest_action(guest_id: int, action: Literal["start","stop","reboot"]) -> dict[str, str]`; `pve.guest_exec(guest_id: int, argv: list[str]) -> dict[str, object]`; `zfs.status() -> dict[str, object]`; `zfs.snapshot(dataset: str, label: str) -> str` returning the full snapshot name; `metrics.host() -> dict[str, float]` with keys `load1`, `mem_used_gb`, `mem_total_gb`, `arc_gb`, `uptime_s`. Allowlisted exec commands are `pve.ALLOWED_EXEC: frozenset[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/hostctl/test_actions.py
import pytest
from fastapi.testclient import TestClient
from hostctl.app import app
from hostctl import pve

AUTH = {"Authorization": "Bearer testtoken"}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("HOSTCTL_TOKEN", "testtoken")


def test_action_on_vm_200_is_403():
    r = TestClient(app).post("/guest/200/action", json={"action": "stop"}, headers=AUTH)
    assert r.status_code == 403


def test_unknown_action_is_422():
    r = TestClient(app).post("/guest/101/action", json={"action": "destroy"}, headers=AUTH)
    assert r.status_code == 422


def test_reboot_invokes_pct(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(pve, "_run", lambda argv: seen.append(argv) or "")
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")
    r = TestClient(app).post("/guest/101/action", json={"action": "reboot"}, headers=AUTH)
    assert r.status_code == 200
    assert seen == [["pct", "reboot", "101"]]


def test_exec_rejects_command_outside_allowlist(monkeypatch):
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")
    r = TestClient(app).post(
        "/guest/101/exec", json={"argv": ["rm", "-rf", "/"]}, headers=AUTH
    )
    assert r.status_code == 403
```

```python
# tests/hostctl/test_zfs.py
from hostctl import zfs


def test_snapshot_returns_full_name(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(zfs, "_run", lambda argv: seen.append(argv) or "")
    name = zfs.snapshot("tank/immich", "pre-upgrade")
    assert name.startswith("tank/immich@pre-upgrade-")
    assert seen[0][:2] == ["zfs", "snapshot"]


def test_no_destroy_verb_exists():
    assert not [n for n in dir(zfs) if "destroy" in n.lower()]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/hostctl -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hostctl.zfs'`

- [ ] **Step 3: Write minimal implementation**

```python
# hostctl/zfs.py
import datetime as dt
import json
import subprocess

POOL = "tank"


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=60).stdout


def status() -> dict[str, object]:
    pool = _run(["zpool", "status", "-v", POOL])
    listing = _run(["zfs", "list", "-H", "-o", "name,used,avail,refer", "-t", "filesystem"])
    datasets = [
        dict(zip(("name", "used", "avail", "refer"), line.split("\t")))
        for line in listing.strip().splitlines()
    ]
    return {"pool_status": pool, "datasets": datasets}


def snapshot(dataset: str, label: str) -> str:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{dataset}@{label}-{stamp}"
    _run(["zfs", "snapshot", name])
    return name


def scrub() -> dict[str, str]:
    _run(["zpool", "scrub", POOL])
    return {"pool": POOL, "scrub": "started"}
```

```python
# hostctl/metrics.py
import os
import subprocess


def host() -> dict[str, float]:
    load1 = os.getloadavg()[0]
    meminfo = {
        k.strip(): float(v.split()[0]) / 1_048_576
        for k, v in (
            line.split(":", 1) for line in open("/proc/meminfo").read().splitlines()
        )
    }
    arc = subprocess.run(
        ["awk", "/^size/ {print $3}", "/proc/spl/kstat/zfs/arcstats"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "load1": load1,
        "mem_total_gb": round(meminfo["MemTotal"], 2),
        "mem_used_gb": round(meminfo["MemTotal"] - meminfo["MemAvailable"], 2),
        "arc_gb": round(float(arc) / 1_073_741_824, 2) if arc else 0.0,
        "uptime_s": float(open("/proc/uptime").read().split()[0]),
    }
```

Append to `hostctl/pve.py`:

```python
ALLOWED_EXEC: frozenset[str] = frozenset(
    {"systemctl", "docker", "journalctl", "df", "free", "uptime", "ss", "curl"}
)


def _kind_of(guest_id: int) -> str:
    for g in _raw_guests():
        if g["id"] == guest_id:
            return g["kind"]
    raise ValueError(f"unknown guest {guest_id}")


def guest_action(guest_id: int, action: str) -> dict[str, str]:
    cmd = "pct" if _kind_of(guest_id) == "lxc" else "qm"
    _run([cmd, action, str(guest_id)])
    return {"guest": str(guest_id), "action": action, "result": "ok"}


def guest_exec(guest_id: int, argv: list[str]) -> dict[str, object]:
    if not argv or argv[0] not in ALLOWED_EXEC:
        raise PermissionError(f"command not allowed: {argv[:1]}")
    out = _run(["pct", "exec", str(guest_id), "--", *argv])
    return {"guest": guest_id, "argv": argv, "stdout": out}
```

Append to `hostctl/app.py`:

```python
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel
from hostctl import metrics, zfs
from hostctl.pve import BLOCKED_GUEST_IDS, guest_action, guest_exec


class ActionBody(BaseModel):
    action: Literal["start", "stop", "reboot"]


class ExecBody(BaseModel):
    argv: list[str]


class SnapshotBody(BaseModel):
    dataset: str
    label: str


def _guard(guest_id: int) -> None:
    if guest_id in BLOCKED_GUEST_IDS:
        raise HTTPException(status_code=403, detail="guest is out of scope")


@app.post("/guest/{guest_id}/action", dependencies=[Depends(_auth)])
def action(guest_id: int, body: ActionBody) -> dict[str, str]:
    _guard(guest_id)
    return guest_action(guest_id, body.action)


@app.post("/guest/{guest_id}/exec", dependencies=[Depends(_auth)])
def execute(guest_id: int, body: ExecBody) -> dict[str, object]:
    _guard(guest_id)
    try:
        return guest_exec(guest_id, body.argv)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/zfs/status", dependencies=[Depends(_auth)])
def zfs_status() -> dict[str, object]:
    return zfs.status()


@app.post("/zfs/snapshot", dependencies=[Depends(_auth)])
def zfs_snapshot(body: SnapshotBody) -> dict[str, str]:
    return {"snapshot": zfs.snapshot(body.dataset, body.label)}


@app.post("/zfs/scrub", dependencies=[Depends(_auth)])
def zfs_scrub() -> dict[str, str]:
    return zfs.scrub()


@app.get("/host/metrics", dependencies=[Depends(_auth)])
def host_metrics() -> dict[str, float]:
    return metrics.host()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/hostctl -v`
Expected: 8 passed

- [ ] **Step 5: Deploy `hostctl` to the Proxmox host**

Write locally then copy — do not heredoc through SSH.

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'mkdir -p /opt/hostctl /etc/hostctl'
rsync -a hostctl/ root@10.0.0.2:/opt/hostctl/hostctl/
scp pyproject.toml root@10.0.0.2:/opt/hostctl/
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'python3 -m venv /opt/hostctl/.venv && /opt/hostctl/.venv/bin/pip install fastapi uvicorn'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'openssl rand -hex 32 > /etc/hostctl/token && chmod 600 /etc/hostctl/token'
```

`deploy/hostctl.service`:

```ini
[Unit]
Description=hostctl - allowlisted Proxmox control surface
After=network-online.target

[Service]
WorkingDirectory=/opt/hostctl
Environment=HOSTCTL_TOKEN_FILE=/etc/hostctl/token
ExecStartPre=/bin/sh -c 'echo HOSTCTL_TOKEN=$(cat /etc/hostctl/token) > /run/hostctl.env'
EnvironmentFile=/run/hostctl.env
ExecStart=/opt/hostctl/.venv/bin/uvicorn hostctl.app:app --host 10.0.0.2 --port 8710
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
scp deploy/hostctl.service root@10.0.0.2:/etc/systemd/system/
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'systemctl daemon-reload && systemctl enable --now hostctl && systemctl is-active hostctl'
```

Expected: `active`

- [ ] **Step 6: Verify the boundary on the live host**

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'T=$(cat /etc/hostctl/token); curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" -d "{\"action\":\"stop\"}" http://10.0.0.2:8710/guest/200/action'
```

Expected: `403`

- [ ] **Step 7: Commit**

```bash
git add hostctl tests/hostctl deploy/hostctl.service
git commit -m "feat(hostctl): actions, zfs, metrics, exec allowlist, systemd unit"
```

---

### Task 3: LXC 103 and the agent skeleton

**Files:**
- Create: `deploy/create-lxc-103.sh`
- Create: `agent/config.py`
- Create: `agent/store.py`
- Create: `tests/agent/test_store.py`

**Interfaces:**
- Consumes: nothing from earlier tasks at runtime.
- Produces: `agent.config.Settings` (a frozen dataclass with fields `minimax_api_key: str`, `minimax_base_url: str`, `model: str`, `hostctl_url: str`, `hostctl_token: str`, `slack_bot_token: str`, `slack_app_token: str`, `db_path: str`, `brain_path: str`) and `agent.config.load() -> Settings`. `agent.store.Store` with methods `record_event(kind: str, payload: dict) -> int`, `last_snapshot() -> dict | None`, `put_snapshot(snapshot: dict) -> None`, `queue_pending(diff: dict) -> None`, `drain_pending() -> list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_store.py
from agent.store import Store


def test_snapshot_round_trip(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    assert s.last_snapshot() is None
    s.put_snapshot({"guests": [{"id": 101, "status": "running"}]})
    assert s.last_snapshot() == {"guests": [{"id": 101, "status": "running"}]}


def test_pending_queue_drains_once(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.queue_pending({"changed": ["jellyfin"]})
    assert s.drain_pending() == [{"changed": ["jellyfin"]}]
    assert s.drain_pending() == []


def test_events_are_append_only(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    first = s.record_event("tool_call", {"tool": "guests_list"})
    second = s.record_event("tool_call", {"tool": "zfs_report"})
    assert second == first + 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/config.py
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    minimax_api_key: str
    minimax_base_url: str
    model: str
    hostctl_url: str
    hostctl_token: str
    slack_bot_token: str
    slack_app_token: str
    db_path: str
    brain_path: str


def _req(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required env var {name}")
    return value


def load() -> Settings:
    return Settings(
        minimax_api_key=_req("MINIMAX_API_KEY"),
        minimax_base_url=os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1"),
        model=os.environ.get("MINIMAX_MODEL", "MiniMax-M3"),
        hostctl_url=os.environ.get("HOSTCTL_URL", "http://10.0.0.2:8710"),
        hostctl_token=_req("HOSTCTL_TOKEN"),
        slack_bot_token=_req("SLACK_BOT_TOKEN"),
        slack_app_token=_req("SLACK_APP_TOKEN"),
        db_path=os.environ.get("AGENT_DB", "/tank/dev/agent/agent.db"),
        brain_path=os.environ.get("AGENT_BRAIN", "/tank/dev/agent/brain.md"),
    )
```

```python
# agent/store.py
import datetime as dt
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS threads (
  thread_ts TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  history TEXT NOT NULL
);
"""


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class Store:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def record_event(self, kind: str, payload: dict) -> int:
        cur = self._db.execute(
            "INSERT INTO events (at, kind, payload) VALUES (?, ?, ?)",
            (_now(), kind, json.dumps(payload)),
        )
        self._db.commit()
        return int(cur.lastrowid or 0)

    def put_snapshot(self, snapshot: dict) -> None:
        self._db.execute(
            "INSERT INTO snapshots (at, body) VALUES (?, ?)", (_now(), json.dumps(snapshot))
        )
        self._db.commit()

    def last_snapshot(self) -> dict | None:
        row = self._db.execute("SELECT body FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def queue_pending(self, diff: dict) -> None:
        self._db.execute(
            "INSERT INTO pending (at, body) VALUES (?, ?)", (_now(), json.dumps(diff))
        )
        self._db.commit()

    def drain_pending(self) -> list[dict]:
        rows = self._db.execute("SELECT id, body FROM pending ORDER BY id").fetchall()
        if not rows:
            return []
        self._db.execute("DELETE FROM pending WHERE id <= ?", (rows[-1][0],))
        self._db.commit()
        return [json.loads(body) for _, body in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_store.py -v`
Expected: 3 passed

- [ ] **Step 5: Create LXC 103 on the host**

`deploy/create-lxc-103.sh`, run on the Proxmox host:

```bash
#!/usr/bin/env bash
set -euo pipefail

pct create 103 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname agent \
  --cores 2 --memory 2048 --swap 512 \
  --rootfs local-lvm:16 \
  --net0 name=eth0,bridge=vmbr0,ip=10.0.0.167/24,gw=10.0.0.254 \
  --features nesting=1 \
  --onboot 1 --unprivileged 1

pct set 103 -mp0 /tank/dev/agent,mp=/tank/dev/agent
mkdir -p /tank/dev/agent
pct start 103
pct exec 103 -- apt-get update
pct exec 103 -- apt-get install -y python3 python3-venv python3-pip
```

```bash
scp deploy/create-lxc-103.sh root@10.0.0.2:/root/
ssh -n -o BatchMode=yes root@10.0.0.2 'bash /root/create-lxc-103.sh && pct status 103'
```

Expected: `status: running`

- [ ] **Step 6: Commit**

```bash
git add agent tests/agent deploy/create-lxc-103.sh
git commit -m "feat(agent): LXC 103, settings and sqlite store"
```

---

### Task 4: MiniMax client, tool registry and the concurrency semaphore

**Files:**
- Create: `agent/tools/base.py`
- Create: `agent/model.py`
- Create: `tests/agent/test_registry.py`
- Create: `tests/agent/test_model.py`

**Interfaces:**
- Consumes: `agent.config.Settings`, `agent.store.Store`.
- Produces: decorator `agent.tools.base.tool(name: str, description: str, schema: dict)` registering into `agent.tools.base.REGISTRY: dict[str, Tool]`; `Tool` is a dataclass with `name`, `description`, `schema`, `fn`. `agent.tools.base.dispatch(name: str, args: dict) -> dict` which validates and returns `{"ok": bool, "result"|"error": ...}`. `agent.tools.base.openai_schema() -> list[dict]`. `agent.model.Agent(settings, store)` with `async run(prompt: str, *, priority: Literal["family","daemon"], system: str) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_registry.py
import pytest
from agent.tools import base


@pytest.fixture(autouse=True)
def _clean():
    base.REGISTRY.clear()
    yield
    base.REGISTRY.clear()


def test_dispatch_rejects_unknown_tool():
    out = base.dispatch("nope", {})
    assert out["ok"] is False and "unknown tool" in out["error"]


def test_dispatch_rejects_bad_arguments():
    @base.tool(
        "echo",
        "echo a string",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )
    def _echo(text: str) -> dict:
        return {"text": text}

    out = base.dispatch("echo", {"wrong": 1})
    assert out["ok"] is False
    assert "text" in out["error"]


def test_dispatch_returns_result_on_valid_call():
    @base.tool(
        "echo",
        "echo a string",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )
    def _echo(text: str) -> dict:
        return {"text": text}

    assert base.dispatch("echo", {"text": "hi"}) == {"ok": True, "result": {"text": "hi"}}


def test_tool_exception_becomes_typed_error():
    @base.tool("boom", "raises", {"type": "object", "properties": {}})
    def _boom() -> dict:
        raise RuntimeError("kaboom")

    out = base.dispatch("boom", {})
    assert out["ok"] is False and "kaboom" in out["error"]
```

```python
# tests/agent/test_model.py
import asyncio
from agent.model import Agent


def test_family_priority_preempts_daemon(monkeypatch, tmp_path):
    from agent.config import Settings
    from agent.store import Store

    settings = Settings(
        minimax_api_key="k", minimax_base_url="http://x/v1", model="MiniMax-M3",
        hostctl_url="http://h", hostctl_token="t", slack_bot_token="b",
        slack_app_token="a", db_path=str(tmp_path / "d.db"), brain_path=str(tmp_path / "b.md"),
    )
    agent = Agent(settings, Store(settings.db_path))
    order: list[str] = []

    async def fake_call(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return "done"

    monkeypatch.setattr(agent, "_complete", fake_call)

    async def scenario():
        daemons = [
            asyncio.create_task(agent.run("d", priority="daemon", system="s"))
            for _ in range(4)
        ]
        await asyncio.sleep(0.01)
        fam = asyncio.create_task(agent.run("f", priority="family", system="s"))
        await fam
        order.append("family")
        await asyncio.gather(*daemons)
        order.append("daemons")

    asyncio.run(scenario())
    assert order == ["family", "daemons"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_registry.py tests/agent/test_model.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.tools'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/tools/base.py
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jsonschema


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    fn: Callable[..., dict[str, Any]]


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, schema: dict[str, Any]):
    def wrap(fn: Callable[..., dict[str, Any]]):
        REGISTRY[name] = Tool(name=name, description=description, schema=schema, fn=fn)
        return fn

    return wrap


def openai_schema() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.schema},
        }
        for t in REGISTRY.values()
    ]


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    entry = REGISTRY.get(name)
    if entry is None:
        return {"ok": False, "error": f"unknown tool: {name}"}
    try:
        jsonschema.validate(args, entry.schema)
    except jsonschema.ValidationError as exc:
        return {"ok": False, "error": f"invalid arguments: {exc.message}"}
    try:
        return {"ok": True, "result": entry.fn(**args)}
    except Exception as exc:  # surfaced to the model as a typed error, never raised through
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
```

```python
# agent/model.py
import asyncio
import json
from typing import Literal

from openai import AsyncOpenAI

from agent.config import Settings
from agent.store import Store
from agent.tools.base import dispatch, openai_schema

MAX_CONCURRENCY = 4
FAMILY_RESERVED = 2
MAX_TOOL_ROUNDS = 12


class Agent:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._s = settings
        self._store = store
        self._client = AsyncOpenAI(
            api_key=settings.minimax_api_key, base_url=settings.minimax_base_url
        )
        self._all = asyncio.Semaphore(MAX_CONCURRENCY)
        self._daemon = asyncio.Semaphore(MAX_CONCURRENCY - FAMILY_RESERVED)

    async def _complete(self, messages: list[dict]) -> str:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = await self._client.chat.completions.create(
                model=self._s.model, messages=messages, tools=openai_schema()
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return msg.content or ""
            messages.append(msg.model_dump(exclude_none=True))
            for call in msg.tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    out = {"ok": False, "error": f"arguments were not valid JSON: {exc}"}
                else:
                    out = dispatch(call.function.name, args)
                self._store.record_event(
                    "tool_call", {"tool": call.function.name, "args": args, "out": out}
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(out),
                    }
                )
        return "stopped: exceeded the tool-call round limit"

    async def run(
        self, prompt: str, *, priority: Literal["family", "daemon"], system: str
    ) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        if priority == "daemon":
            async with self._daemon, self._all:
                return await self._complete(messages)
        async with self._all:
            return await self._complete(messages)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add agent/tools agent/model.py tests/agent
git commit -m "feat(agent): tool registry with schema validation and priority semaphore"
```

---

### Task 5: Infrastructure tools

**Files:**
- Create: `agent/clients.py`
- Create: `agent/tools/infra.py`
- Create: `tests/agent/test_infra_tools.py`

**Interfaces:**
- Consumes: `agent.tools.base.tool`, `agent.config.Settings`.
- Produces: `agent.clients.hostctl_get(path: str) -> dict`, `agent.clients.hostctl_post(path: str, body: dict) -> dict`, `agent.clients.service_get(base: str, path: str, headers: dict | None = None) -> dict`. Registered tools: `guests_list`, `guest_action`, `zfs_report`, `zfs_snapshot`, `docker_stacks`, `docker_action`, `monitors_status`, `host_metrics`, `adguard_report`.

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_infra_tools.py
import httpx
import pytest
import respx

from agent.tools import base, infra

AGENT_HOSTCTL = "http://10.0.0.2:8710"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HOSTCTL_URL", AGENT_HOSTCTL)
    monkeypatch.setenv("HOSTCTL_TOKEN", "t")


@respx.mock
def test_guests_list_calls_hostctl():
    respx.get(f"{AGENT_HOSTCTL}/guests").mock(
        return_value=httpx.Response(200, json={"guests": [{"id": 101}]})
    )
    assert base.dispatch("guests_list", {}) == {"ok": True, "result": {"guests": [{"id": 101}]}}


@respx.mock
def test_guest_action_forwards_action():
    route = respx.post(f"{AGENT_HOSTCTL}/guest/101/action").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    base.dispatch("guest_action", {"guest": 101, "action": "reboot"})
    assert route.calls.last.request.read() == b'{"action":"reboot"}'


def test_guest_action_rejects_guest_200():
    out = base.dispatch("guest_action", {"guest": 200, "action": "stop"})
    assert out["ok"] is False


def test_guest_action_rejects_unlisted_action():
    out = base.dispatch("guest_action", {"guest": 101, "action": "destroy"})
    assert out["ok"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_infra_tools.py -v`
Expected: FAIL with `ImportError: cannot import name 'infra'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/clients.py
import os
from typing import Any

import httpx

TIMEOUT = httpx.Timeout(30.0)


def _hostctl_base() -> str:
    return os.environ.get("HOSTCTL_URL", "http://10.0.0.2:8710")


def _hostctl_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['HOSTCTL_TOKEN']}"}


def hostctl_get(path: str) -> dict[str, Any]:
    r = httpx.get(f"{_hostctl_base()}{path}", headers=_hostctl_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def hostctl_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    r = httpx.post(
        f"{_hostctl_base()}{path}", json=body, headers=_hostctl_headers(), timeout=TIMEOUT
    )
    r.raise_for_status()
    return r.json()


def service_get(base: str, path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    r = httpx.get(f"{base}{path}", headers=headers or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()
```

```python
# agent/tools/infra.py
import os
from typing import Any

from agent.clients import hostctl_get, hostctl_post, service_get
from agent.tools.base import tool

DOCKER_HOST = "http://10.0.0.165"
BLOCKED_GUESTS = {200}
GUEST_ACTIONS = ("start", "stop", "reboot")
STACK_ACTIONS = ("up", "down", "restart", "pull")

NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


@tool("guests_list", "List Proxmox guests and their status.", NO_ARGS)
def guests_list() -> dict[str, Any]:
    return hostctl_get("/guests")


@tool(
    "guest_action",
    "Start, stop or reboot a Proxmox guest by numeric id.",
    {
        "type": "object",
        "properties": {
            "guest": {"type": "integer"},
            "action": {"type": "string", "enum": list(GUEST_ACTIONS)},
        },
        "required": ["guest", "action"],
        "additionalProperties": False,
    },
)
def guest_action(guest: int, action: str) -> dict[str, Any]:
    if guest in BLOCKED_GUESTS:
        raise PermissionError(f"guest {guest} is out of scope")
    return hostctl_post(f"/guest/{guest}/action", {"action": action})


@tool("zfs_report", "Pool health, dataset usage and free space for tank.", NO_ARGS)
def zfs_report() -> dict[str, Any]:
    return hostctl_get("/zfs/status")


@tool(
    "zfs_snapshot",
    "Take a ZFS snapshot of a dataset. Snapshots are created, never deleted.",
    {
        "type": "object",
        "properties": {"dataset": {"type": "string"}, "label": {"type": "string"}},
        "required": ["dataset", "label"],
        "additionalProperties": False,
    },
)
def zfs_snapshot(dataset: str, label: str) -> dict[str, Any]:
    return hostctl_post("/zfs/snapshot", {"dataset": dataset, "label": label})


@tool("host_metrics", "Proxmox host load, memory, ARC size and uptime.", NO_ARGS)
def host_metrics() -> dict[str, Any]:
    return hostctl_get("/host/metrics")


@tool("docker_stacks", "List Dockge stacks in LXC 101 and their state.", NO_ARGS)
def docker_stacks() -> dict[str, Any]:
    return hostctl_post("/guest/101/exec", {"argv": ["docker", "compose", "ls", "--format", "json"]})


@tool(
    "docker_action",
    "Bring a Dockge stack up, down, restart it, or pull new images.",
    {
        "type": "object",
        "properties": {
            "stack": {"type": "string"},
            "action": {"type": "string", "enum": list(STACK_ACTIONS)},
        },
        "required": ["stack", "action"],
        "additionalProperties": False,
    },
)
def docker_action(stack: str, action: str) -> dict[str, Any]:
    verb = {"up": ["up", "-d"], "down": ["down"], "restart": ["restart"], "pull": ["pull"]}[action]
    return hostctl_post(
        "/guest/101/exec",
        {"argv": ["docker", "compose", "-f", f"/opt/stacks/{stack}/compose.yaml", *verb]},
    )


@tool("monitors_status", "Uptime Kuma monitor states.", NO_ARGS)
def monitors_status() -> dict[str, Any]:
    return service_get(
        f"{DOCKER_HOST}:3001",
        "/api/status-page/heartbeat/homelab",
    )


@tool("adguard_report", "AdGuard Home query and blocking stats.", NO_ARGS)
def adguard_report() -> dict[str, Any]:
    token = os.environ["ADGUARD_BASIC_AUTH"]
    return service_get(f"{DOCKER_HOST}:8080", "/control/stats", {"Authorization": f"Basic {token}"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_infra_tools.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add agent/clients.py agent/tools/infra.py tests/agent/test_infra_tools.py
git commit -m "feat(agent): infrastructure tools over hostctl and service APIs"
```

---

### Task 6: Slack Socket Mode

**Files:**
- Create: `agent/tools/comms.py`
- Create: `agent/slack_app.py`
- Create: `tests/agent/test_slack.py`

**Interfaces:**
- Consumes: `agent.model.Agent`, `agent.store.Store`, `agent.tools.base.tool`.
- Produces: registered tool `slack_say(channel: str, text: str) -> dict`. `agent.slack_app.build(agent: Agent, store: Store, bot_token: str) -> AsyncApp`. Channel constants `agent.slack_app.CH_HOMELAB = "#homelab"`, `CH_FAMILY = "#family"`, `CH_LOG = "#agent-log"`. `agent.slack_app.SYSTEM_FAMILY: str` is the family-facing system prompt.

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_slack.py
import asyncio
from agent import slack_app


class FakeAgent:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def run(self, prompt, *, priority, system):
        self.calls.append((prompt, priority))
        return f"answered: {prompt}"


def test_mention_runs_with_family_priority():
    agent = FakeAgent()
    said: list[dict] = []

    async def say(**kwargs):
        said.append(kwargs)

    asyncio.run(
        slack_app.handle_message(
            agent=agent,
            text="<@U123> is jellyfin up?",
            thread_ts="1.1",
            say=say,
        )
    )
    assert agent.calls[0][1] == "family"
    assert said[0]["thread_ts"] == "1.1"
    assert "answered" in said[0]["text"]


def test_mention_strips_the_bot_handle():
    agent = FakeAgent()

    async def say(**kwargs):
        return None

    asyncio.run(
        slack_app.handle_message(agent=agent, text="<@U123> hello", thread_ts="1.1", say=say)
    )
    assert agent.calls[0][0] == "hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_slack.py -v`
Expected: FAIL with `ImportError: cannot import name 'slack_app'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/tools/comms.py
import os
from typing import Any

import httpx

from agent.tools.base import tool


@tool(
    "slack_say",
    "Post a message to a Slack channel. Use #homelab for actions and incidents, "
    "#family for family-facing replies.",
    {
        "type": "object",
        "properties": {"channel": {"type": "string"}, "text": {"type": "string"}},
        "required": ["channel", "text"],
        "additionalProperties": False,
    },
)
def slack_say(channel: str, text: str) -> dict[str, Any]:
    r = httpx.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
        json={"channel": channel, "text": text},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()
```

```python
# agent/slack_app.py
import re
from typing import Any, Protocol

from slack_bolt.app.async_app import AsyncApp

from agent.store import Store

CH_HOMELAB = "#homelab"
CH_FAMILY = "#family"
CH_LOG = "#agent-log"

MENTION = re.compile(r"<@[A-Z0-9]+>\s*")

SYSTEM_FAMILY = (
    "You are the household assistant for Tech's family, running on their home server. "
    "You manage the homelab and answer questions for everyone in the house. "
    "You act on your own judgement without asking permission. "
    "Prefer doing the thing over describing how to do it. "
    "Answer plainly and briefly. Family members are not engineers. "
    "You have no access to the trading VM and must never discuss placing trades."
)


class Runner(Protocol):
    async def run(self, prompt: str, *, priority: str, system: str) -> str: ...


async def handle_message(*, agent: Runner, text: str, thread_ts: str, say: Any) -> None:
    prompt = MENTION.sub("", text).strip()
    answer = await agent.run(prompt, priority="family", system=SYSTEM_FAMILY)
    await say(text=answer, thread_ts=thread_ts)


def build(agent: Runner, store: Store, bot_token: str) -> AsyncApp:
    app = AsyncApp(token=bot_token)

    @app.event("app_mention")
    async def _mention(event: dict, say: Any) -> None:
        store.record_event("slack_mention", {"user": event.get("user"), "text": event.get("text")})
        await handle_message(
            agent=agent,
            text=event.get("text", ""),
            thread_ts=event.get("thread_ts") or event["ts"],
            say=say,
        )

    @app.event("message")
    async def _dm(event: dict, say: Any) -> None:
        if event.get("channel_type") != "im" or event.get("bot_id"):
            return
        store.record_event("slack_dm", {"user": event.get("user"), "text": event.get("text")})
        await handle_message(
            agent=agent,
            text=event.get("text", ""),
            thread_ts=event.get("thread_ts") or event["ts"],
            say=say,
        )

    return app
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_slack.py -v`
Expected: 2 passed

- [ ] **Step 5: Create the Slack app**

Manual, once, at `https://api.slack.com/apps`:

1. Create app from manifest, enable **Socket Mode**.
2. Bot scopes: `app_mentions:read`, `channels:history`, `channels:read`, `chat:write`, `files:write`, `im:history`, `im:read`, `im:write`, `users:read`.
3. Event subscriptions: `app_mention`, `message.im`, `message.channels`.
4. Install to workspace. Copy the bot token (`xoxb-`) and app token (`xapp-`).
5. Create `#homelab`, `#family`, `#agent-log`. Invite the bot to all three, invite family to `#family`.

Socket Mode means no public ingress. The Vodafone router needs no port forward.

- [ ] **Step 6: Commit**

```bash
git add agent/slack_app.py agent/tools/comms.py tests/agent/test_slack.py
git commit -m "feat(agent): slack socket mode listener and posting tool"
```

---

### Task 7: The tick, state diffing and degraded mode

**Files:**
- Create: `agent/tick.py`
- Create: `agent/main.py`
- Create: `tests/agent/test_tick.py`

**Interfaces:**
- Consumes: `agent.model.Agent`, `agent.store.Store`, tools from `agent.tools.infra`.
- Produces: `agent.tick.collect() -> dict`, `agent.tick.diff(old: dict | None, new: dict) -> dict` returning `{"changed": list[str], "details": dict}`, `agent.tick.Ticker(agent, store, notify)` with `async once() -> None`. `notify` is `Callable[[str, str], Awaitable[None]]` taking channel and text. `agent.tick.SYSTEM_DAEMON: str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_tick.py
import asyncio
from agent import tick


def test_diff_detects_status_change():
    old = {"guests": {"101": "running"}, "zfs": {"state": "ONLINE"}}
    new = {"guests": {"101": "stopped"}, "zfs": {"state": "ONLINE"}}
    assert tick.diff(old, new)["changed"] == ["guests.101"]


def test_diff_of_identical_state_is_empty():
    state = {"guests": {"101": "running"}}
    assert tick.diff(state, state)["changed"] == []


def test_first_run_is_not_reported_as_change():
    assert tick.diff(None, {"guests": {"101": "running"}})["changed"] == []


class SilentAgent:
    def __init__(self):
        self.runs = 0

    async def run(self, prompt, *, priority, system):
        self.runs += 1
        return "ok"


def test_idle_tick_makes_no_model_call(tmp_path, monkeypatch):
    from agent.store import Store

    store = Store(str(tmp_path / "t.db"))
    agent = SilentAgent()
    monkeypatch.setattr(tick, "collect", lambda: {"guests": {"101": "running"}})
    ticker = tick.Ticker(agent, store, notify=_noop)
    asyncio.run(ticker.once())
    asyncio.run(ticker.once())
    assert agent.runs == 0


def test_changed_tick_calls_the_model(tmp_path, monkeypatch):
    from agent.store import Store

    store = Store(str(tmp_path / "t.db"))
    agent = SilentAgent()
    states = [{"guests": {"101": "running"}}, {"guests": {"101": "stopped"}}]
    monkeypatch.setattr(tick, "collect", lambda: states.pop(0))
    ticker = tick.Ticker(agent, store, notify=_noop)
    asyncio.run(ticker.once())
    asyncio.run(ticker.once())
    assert agent.runs == 1


def test_model_failure_queues_the_diff(tmp_path, monkeypatch):
    from agent.store import Store

    class Broken:
        async def run(self, prompt, *, priority, system):
            raise RuntimeError("provider down")

    store = Store(str(tmp_path / "t.db"))
    states = [{"guests": {"101": "running"}}, {"guests": {"101": "stopped"}}]
    monkeypatch.setattr(tick, "collect", lambda: states.pop(0))
    ticker = tick.Ticker(Broken(), store, notify=_noop)
    asyncio.run(ticker.once())
    asyncio.run(ticker.once())
    assert store.drain_pending() != []


async def _noop(channel: str, text: str) -> None:
    return None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_tick.py -v`
Expected: FAIL with `ImportError: cannot import name 'tick'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/tick.py
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from agent.store import Store
from agent.tools import infra

SYSTEM_DAEMON = (
    "You are the autonomous operator of Tech's home server. "
    "You have just been woken because the homelab state changed. "
    "Diagnose the change and fix it yourself using your tools. Do not ask permission. "
    "Post what you did and why to #homelab using slack_say, in this shape:\n"
    ":wrench: <what you did>\n  why: <evidence>\n  result: <outcome>\n"
    "If nothing needs doing, call no tools and reply with the single word: idle. "
    "Snapshot a dataset before any change that touches its contents. "
    "You have no access to the trading VM."
)


class Runner(Protocol):
    async def run(self, prompt: str, *, priority: str, system: str) -> str: ...


def collect() -> dict[str, Any]:
    guests = {str(g["id"]): g["status"] for g in infra.guests_list()["guests"]}
    zfs = infra.zfs_report()
    metrics = infra.host_metrics()
    return {
        "guests": guests,
        "zfs_pool": zfs["pool_status"],
        "zfs_datasets": zfs["datasets"],
        "host": metrics,
    }


def _flatten(node: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(node, dict):
        out: dict[str, str] = {}
        for key, value in node.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: json.dumps(node, sort_keys=True)}


def diff(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    if old is None:
        return {"changed": [], "details": {}}
    a, b = _flatten(old), _flatten(new)
    changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return {"changed": changed, "details": {k: {"was": a.get(k), "now": b.get(k)} for k in changed}}


class Ticker:
    def __init__(
        self, agent: Runner, store: Store, notify: Callable[[str, str], Awaitable[None]]
    ) -> None:
        self._agent = agent
        self._store = store
        self._notify = notify
        self._degraded = False

    async def once(self) -> None:
        state = collect()
        previous = self._store.last_snapshot()
        self._store.put_snapshot(state)
        delta = diff(previous, state)
        backlog = self._store.drain_pending()
        if not delta["changed"] and not backlog:
            return
        prompt = json.dumps(
            {"state": state, "change": delta, "backlog": backlog}, indent=2, sort_keys=True
        )
        try:
            answer = await self._agent.run(prompt, priority="daemon", system=SYSTEM_DAEMON)
        except Exception as exc:
            for item in [delta, *backlog]:
                self._store.queue_pending(item)
            if not self._degraded:
                self._degraded = True
                await self._notify(
                    "#homelab",
                    f":warning: agent degraded, model provider unreachable ({exc}). "
                    "Still watching, changes are queued.",
                )
            return
        if self._degraded:
            self._degraded = False
            await self._notify("#homelab", ":white_check_mark: agent recovered, backlog drained.")
        self._store.record_event("tick", {"change": delta, "answer": answer})
```

```python
# agent/main.py
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from agent import config, slack_app, tick
from agent.model import Agent
from agent.store import Store
from agent.tools import comms, infra  # noqa: F401  imported for tool registration

TICK_SECONDS = 60


async def amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = config.load()
    store = Store(settings.db_path)
    agent = Agent(settings, store)
    app = slack_app.build(agent, store, settings.slack_bot_token)

    async def notify(channel: str, text: str) -> None:
        await app.client.chat_postMessage(channel=channel, text=text)

    ticker = tick.Ticker(agent, store, notify)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(ticker.once, "interval", seconds=TICK_SECONDS, max_instances=1)
    scheduler.start()

    await notify(slack_app.CH_HOMELAB, ":satellite: homelab agent online")
    await AsyncSocketModeHandler(app, settings.slack_app_token).start_async()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_tick.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add agent/tick.py agent/main.py tests/agent/test_tick.py
git commit -m "feat(agent): 60s tick with state diffing and degraded-mode queueing"
```

---

### Task 8: Media tools

**Files:**
- Create: `agent/tools/media.py`
- Create: `tests/agent/test_media_tools.py`

**Interfaces:**
- Consumes: `agent.clients.service_get`, `agent.tools.base.tool`.
- Produces: registered tools `media_search(query: str) -> dict`, `media_request(query: str, kind: Literal["movie","show"]) -> dict`, `media_library_status() -> dict`.

Jellyfin 12 requires `Authorization: MediaBrowser Token="KEY"`. The `?api_key=` form does not work.

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_media_tools.py
import httpx
import pytest
import respx

from agent.tools import base, media  # noqa: F401


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JELLYFIN_KEY", "jkey")
    monkeypatch.setenv("JELLYSEERR_KEY", "skey")


@respx.mock
def test_jellyfin_uses_mediabrowser_auth_header():
    route = respx.get("http://10.0.0.165:8096/Items").mock(
        return_value=httpx.Response(200, json={"Items": []})
    )
    base.dispatch("media_search", {"query": "dune"})
    auth = route.calls.last.request.headers["Authorization"]
    assert auth == 'MediaBrowser Token="jkey"'


@respx.mock
def test_request_posts_to_jellyseerr_with_media_type():
    route = respx.post("http://10.0.0.165:5055/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 7})
    )
    respx.get("http://10.0.0.165:5055/api/v1/search").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 42, "mediaType": "movie"}]})
    )
    out = base.dispatch("media_request", {"query": "dune", "kind": "movie"})
    assert out["ok"] is True
    assert b'"mediaType":"movie"' in route.calls.last.request.read()


def test_request_rejects_unknown_kind():
    assert base.dispatch("media_request", {"query": "x", "kind": "album"})["ok"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_media_tools.py -v`
Expected: FAIL with `ImportError: cannot import name 'media'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/tools/media.py
import os
from typing import Any

import httpx

from agent.clients import TIMEOUT, service_get
from agent.tools.base import tool

JELLYFIN = "http://10.0.0.165:8096"
JELLYSEERR = "http://10.0.0.165:5055"
KINDS = ("movie", "show")


def _jf_headers() -> dict[str, str]:
    return {"Authorization": f'MediaBrowser Token="{os.environ["JELLYFIN_KEY"]}"'}


def _js_headers() -> dict[str, str]:
    return {"X-Api-Key": os.environ["JELLYSEERR_KEY"]}


@tool(
    "media_search",
    "Search the Jellyfin library for a film or show that is already available.",
    {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
)
def media_search(query: str) -> dict[str, Any]:
    r = httpx.get(
        f"{JELLYFIN}/Items",
        params={"searchTerm": query, "Recursive": "true", "Limit": 10},
        headers=_jf_headers(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


@tool(
    "media_request",
    "Request a film or show through Jellyseerr so it appears in Jellyfin.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
        },
        "required": ["query", "kind"],
        "additionalProperties": False,
    },
)
def media_request(query: str, kind: str) -> dict[str, Any]:
    found = service_get(JELLYSEERR, f"/api/v1/search?query={query}", _js_headers())
    results = [r for r in found.get("results", []) if r.get("mediaType") == kind]
    if not results:
        return {"requested": False, "reason": f"no {kind} matched {query!r}"}
    r = httpx.post(
        f"{JELLYSEERR}/api/v1/request",
        json={"mediaId": results[0]["id"], "mediaType": kind},
        headers=_js_headers(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return {"requested": True, "jellyseerr": r.json()}


@tool(
    "media_library_status",
    "Jellyfin item counts and pending Jellyseerr requests.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def media_library_status() -> dict[str, Any]:
    counts = service_get(JELLYFIN, "/Items/Counts", _jf_headers())
    pending = service_get(JELLYSEERR, "/api/v1/request?filter=pending", _js_headers())
    return {"jellyfin": counts, "pending_requests": pending}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_media_tools.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add agent/tools/media.py tests/agent/test_media_tools.py
git commit -m "feat(agent): jellyfin and jellyseerr tools"
```

---

### Task 9: Photo tools

**Files:**
- Create: `agent/tools/photos.py`
- Create: `tests/agent/test_photos_tools.py`

**Interfaces:**
- Consumes: `agent.clients.service_get`, `agent.tools.base.tool`.
- Produces: registered tools `photos_search(query: str) -> dict`, `photos_stats() -> dict`.

Immich config saves through its own UI overwrite direct database edits. This module only reads and searches; it never writes Immich configuration.

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_photos_tools.py
import httpx
import pytest
import respx

from agent.tools import base, photos  # noqa: F401


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("IMMICH_KEY", "ikey")


@respx.mock
def test_search_uses_smart_search_endpoint():
    route = respx.post("http://10.0.0.165:2283/api/search/smart").mock(
        return_value=httpx.Response(200, json={"assets": {"items": []}})
    )
    base.dispatch("photos_search", {"query": "the dog on the beach"})
    assert route.calls.last.request.headers["x-api-key"] == "ikey"


@respx.mock
def test_stats_reports_counts():
    respx.get("http://10.0.0.165:2283/api/server/statistics").mock(
        return_value=httpx.Response(200, json={"photos": 41233, "videos": 902})
    )
    out = base.dispatch("photos_stats", {})
    assert out["result"]["photos"] == 41233
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_photos_tools.py -v`
Expected: FAIL with `ImportError: cannot import name 'photos'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/tools/photos.py
import os
from typing import Any

import httpx

from agent.clients import TIMEOUT, service_get
from agent.tools.base import tool

IMMICH = "http://10.0.0.165:2283"


def _headers() -> dict[str, str]:
    return {"x-api-key": os.environ["IMMICH_KEY"]}


@tool(
    "photos_search",
    "Search the family photo library by describing what is in the picture.",
    {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
)
def photos_search(query: str) -> dict[str, Any]:
    r = httpx.post(
        f"{IMMICH}/api/search/smart",
        json={"query": query, "size": 10},
        headers=_headers(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


@tool(
    "photos_stats",
    "Photo and video counts and storage used by Immich.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def photos_stats() -> dict[str, Any]:
    return service_get(IMMICH, "/api/server/statistics", _headers())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_photos_tools.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add agent/tools/photos.py tests/agent/test_photos_tools.py
git commit -m "feat(agent): immich search and stats tools"
```

---

### Task 10: Radicale calendar and household tools

**Files:**
- Create: `deploy/stacks/radicale/compose.yaml`
- Create: `agent/tools/household.py`
- Create: `tests/agent/test_household_tools.py`

**Interfaces:**
- Consumes: `agent.tools.base.tool`.
- Produces: registered tools `calendar_list(days: int) -> dict`, `calendar_add(title: str, start: str, end: str, who: str) -> dict`, `notes_append(list_name: str, item: str) -> dict`. Start and end are ISO 8601 strings. Lists are plain markdown files under `/tank/dev/agent/lists/`.

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_household_tools.py
import pytest

from agent.tools import base, household


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_LISTS", str(tmp_path))
    monkeypatch.setenv("CALDAV_URL", "http://10.0.0.165:5232/family/home/")
    monkeypatch.setenv("CALDAV_USER", "family")
    monkeypatch.setenv("CALDAV_PASSWORD", "pw")


def test_notes_append_creates_and_appends():
    base.dispatch("notes_append", {"list_name": "shopping", "item": "milk"})
    out = base.dispatch("notes_append", {"list_name": "shopping", "item": "bread"})
    assert out["ok"] is True
    assert out["result"]["items"] == ["milk", "bread"]


def test_notes_append_rejects_path_traversal():
    out = base.dispatch("notes_append", {"list_name": "../../etc/passwd", "item": "x"})
    assert out["ok"] is False


def test_calendar_add_rejects_non_iso_dates():
    out = base.dispatch(
        "calendar_add",
        {"title": "dentist", "start": "next tuesday", "end": "later", "who": "mum"},
    )
    assert out["ok"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_household_tools.py -v`
Expected: FAIL with `ImportError: cannot import name 'household'`

- [ ] **Step 3: Write minimal implementation**

```yaml
# deploy/stacks/radicale/compose.yaml
services:
  radicale:
    image: tomsquest/docker-radicale:latest
    container_name: radicale
    restart: unless-stopped
    ports:
      - "5232:5232"
    volumes:
      - /tank/dev/agent/radicale:/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5232/"]
      interval: 30s
```

```python
# agent/tools/household.py
import datetime as dt
import os
import re
import uuid
from pathlib import Path
from typing import Any

import caldav

from agent.tools.base import tool

SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,40}$")


def _lists_dir() -> Path:
    path = Path(os.environ.get("AGENT_LISTS", "/tank/dev/agent/lists"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _calendar() -> caldav.Calendar:
    client = caldav.DAVClient(
        url=os.environ["CALDAV_URL"],
        username=os.environ["CALDAV_USER"],
        password=os.environ["CALDAV_PASSWORD"],
    )
    return client.principal().calendars()[0]


def _iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


@tool(
    "calendar_list",
    "Family calendar events for the next N days.",
    {
        "type": "object",
        "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 90}},
        "required": ["days"],
        "additionalProperties": False,
    },
)
def calendar_list(days: int) -> dict[str, Any]:
    start = dt.datetime.now(dt.UTC)
    events = _calendar().search(
        start=start, end=start + dt.timedelta(days=days), event=True, expand=True
    )
    return {
        "events": [
            {
                "summary": str(e.vobject_instance.vevent.summary.value),
                "start": str(e.vobject_instance.vevent.dtstart.value),
            }
            for e in events
        ]
    }


@tool(
    "calendar_add",
    "Add an event to the family calendar. Times must be ISO 8601, for example "
    "2026-09-22T14:00:00+02:00.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "who": {"type": "string"},
        },
        "required": ["title", "start", "end", "who"],
        "additionalProperties": False,
    },
)
def calendar_add(title: str, start: str, end: str, who: str) -> dict[str, Any]:
    begins, ends = _iso(start), _iso(end)
    event = _calendar().save_event(
        dtstart=begins, dtend=ends, summary=f"{title} ({who})", uid=str(uuid.uuid4())
    )
    return {"added": True, "uid": str(event.id), "summary": f"{title} ({who})"}


@tool(
    "notes_append",
    "Append an item to a shared household list such as shopping or chores.",
    {
        "type": "object",
        "properties": {"list_name": {"type": "string"}, "item": {"type": "string"}},
        "required": ["list_name", "item"],
        "additionalProperties": False,
    },
)
def notes_append(list_name: str, item: str) -> dict[str, Any]:
    if not SAFE_NAME.match(list_name):
        raise ValueError(f"invalid list name: {list_name!r}")
    path = _lists_dir() / f"{list_name}.md"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"- {item}\n")
    items = [
        line[2:].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("- ")
    ]
    return {"list": list_name, "items": items}
```

`_iso` raising `ValueError` on `"next tuesday"` is what makes the third test pass; `dispatch` converts it to a typed error.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_household_tools.py -v`
Expected: 3 passed

- [ ] **Step 5: Deploy Radicale and create the family account**

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 101 -- mkdir -p /opt/stacks/radicale'
scp deploy/stacks/radicale/compose.yaml root@10.0.0.2:/tmp/radicale-compose.yaml
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct push 101 /tmp/radicale-compose.yaml /opt/stacks/radicale/compose.yaml'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 101 -- docker compose -f /opt/stacks/radicale/compose.yaml up -d'
```

Create the `family` user in Radicale's htpasswd, then subscribe each family phone to
`http://10.0.0.165:5232/family/home/` as a CalDAV account.

- [ ] **Step 6: Commit**

```bash
git add agent/tools/household.py tests/agent/test_household_tools.py deploy/stacks/radicale
git commit -m "feat(agent): radicale calendar and shared household lists"
```

---

### Task 11: Brain, audit trail, deployment and live smoke test

**Files:**
- Create: `agent/brain.py`
- Create: `agent/tools/memory.py`
- Modify: `agent/model.py:60-80`
- Create: `deploy/homelab-agent.service`
- Create: `Makefile`
- Create: `tests/agent/test_brain.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `agent.brain.Brain(path: str)` with `read(topic: str) -> str` and `write(topic: str, content: str) -> None`, storing one `## topic` section per topic in a single markdown file. Registered tools `brain_read(topic: str) -> dict`, `brain_write(topic: str, content: str) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_brain.py
from agent.brain import Brain


def test_write_then_read_a_topic(tmp_path):
    brain = Brain(str(tmp_path / "brain.md"))
    brain.write("jellyfin", "restarts weekly around 3am, cause unknown")
    assert "restarts weekly" in brain.read("jellyfin")


def test_write_replaces_rather_than_duplicates(tmp_path):
    path = tmp_path / "brain.md"
    brain = Brain(str(path))
    brain.write("zfs", "first note")
    brain.write("zfs", "second note")
    assert path.read_text().count("## zfs") == 1
    assert "first note" not in brain.read("zfs")


def test_unknown_topic_reads_empty(tmp_path):
    assert Brain(str(tmp_path / "brain.md")).read("nothing") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_brain.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.brain'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/brain.py
import re
from pathlib import Path


class Brain:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("# Homelab agent brain\n\n", encoding="utf-8")

    def _sections(self) -> dict[str, str]:
        text = self._path.read_text(encoding="utf-8")
        parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
        return {
            parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)
        }

    def read(self, topic: str) -> str:
        return self._sections().get(topic, "")

    def write(self, topic: str, content: str) -> None:
        sections = self._sections()
        sections[topic] = content.strip()
        body = "# Homelab agent brain\n\n" + "\n\n".join(
            f"## {name}\n{text}" for name, text in sorted(sections.items())
        )
        self._path.write_text(body + "\n", encoding="utf-8")
```

```python
# agent/tools/memory.py
import os
from typing import Any

from agent.brain import Brain
from agent.tools.base import tool


def _brain() -> Brain:
    return Brain(os.environ.get("AGENT_BRAIN", "/tank/dev/agent/brain.md"))


@tool(
    "brain_read",
    "Read what you previously recorded about a topic, for example a recurring fault.",
    {
        "type": "object",
        "properties": {"topic": {"type": "string"}},
        "required": ["topic"],
        "additionalProperties": False,
    },
)
def brain_read(topic: str) -> dict[str, Any]:
    return {"topic": topic, "content": _brain().read(topic)}


@tool(
    "brain_write",
    "Record something worth remembering across restarts. Replaces the topic's previous note.",
    {
        "type": "object",
        "properties": {"topic": {"type": "string"}, "content": {"type": "string"}},
        "required": ["topic", "content"],
        "additionalProperties": False,
    },
)
def brain_write(topic: str, content: str) -> dict[str, Any]:
    _brain().write(topic, content)
    return {"topic": topic, "saved": True}
```

Modify `agent/model.py` so every tool call is mirrored to `#agent-log`. Replace the
`self._store.record_event("tool_call", ...)` line in `_complete` with:

```python
                self._store.record_event(
                    "tool_call", {"tool": call.function.name, "args": args, "out": out}
                )
                if self._audit is not None:
                    await self._audit(
                        "#agent-log",
                        f"`{call.function.name}` {json.dumps(args)} -> "
                        f"{'ok' if out['ok'] else out['error']}",
                    )
```

and add `audit: Callable[[str, str], Awaitable[None]] | None = None` to `Agent.__init__`,
stored as `self._audit`. Wire it in `agent/main.py` by constructing `Agent` after
`app`, passing `audit=notify`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -v`
Expected: all tests pass

- [ ] **Step 5: Deploy the agent to LXC 103**

`deploy/homelab-agent.service`:

```ini
[Unit]
Description=Homelab agent
After=network-online.target

[Service]
WorkingDirectory=/opt/homelab-agent
EnvironmentFile=/etc/homelab-agent/env
ExecStart=/opt/homelab-agent/.venv/bin/python -m agent.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

`Makefile`:

```make
HOST := root@10.0.0.2
CT   := 103

test:
	pytest -q && ruff check . && mypy --strict agent hostctl

deploy: test
	tar czf /tmp/agent.tgz agent pyproject.toml
	scp /tmp/agent.tgz $(HOST):/tmp/
	ssh -n $(HOST) 'pct push $(CT) /tmp/agent.tgz /tmp/agent.tgz'
	ssh -n $(HOST) 'pct exec $(CT) -- tar xzf /tmp/agent.tgz -C /opt/homelab-agent'
	ssh -n $(HOST) 'pct exec $(CT) -- systemctl restart homelab-agent'
	ssh -n $(HOST) 'pct exec $(CT) -- systemctl is-active homelab-agent'
```

Populate `/etc/homelab-agent/env` inside LXC 103, mode `0600`:

```
MINIMAX_API_KEY=
HOSTCTL_TOKEN=
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=
JELLYFIN_KEY=
JELLYSEERR_KEY=
IMMICH_KEY=
ADGUARD_BASIC_AUTH=
CALDAV_URL=http://10.0.0.165:5232/family/home/
CALDAV_USER=family
CALDAV_PASSWORD=
```

```bash
make deploy
```

Expected: `active`

- [ ] **Step 6: Live smoke test**

1. `#homelab` shows `:satellite: homelab agent online`.
2. In `#family`, ask the bot `@agent is jellyfin running?` — expect a plain answer inside a minute.
3. Stop a harmless container and watch the agent fix it unprompted:

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 101 -- docker stop uptime-kuma'
```

Expect within two ticks: a `:wrench:` post in `#homelab` naming the container, the
evidence, and the result, plus matching `docker_action` entries in `#agent-log`.

4. Confirm the boundary holds. In `#family`, ask `@agent restart the mt5 vm`. Expect the
   agent to report it has no such capability, and `#agent-log` to show either no tool call
   or a rejected one. It must not reach VM 200.

- [ ] **Step 7: Commit**

```bash
git add agent/brain.py agent/tools/memory.py agent/model.py deploy/homelab-agent.service Makefile tests/agent/test_brain.py
git commit -m "feat(agent): persistent brain, slack audit trail, systemd deployment"
```

---

## Self-review notes

- Spec coverage: autonomy (Tasks 1, 2, 5 — boundary by absence), Slack (Task 6), everything-scope (Tasks 5, 8, 9, 10), tick and degradation (Task 7), memory and audit (Task 11), calendar backend (Task 10). No spec section is unimplemented.
- The spec lists `media_library_status`, `photos_stats`, `zfs_scrub` and `guest_exec`; all are implemented. `slack_say` is registered in Task 6 and used by the daemon prompt in Task 7.
- Deferred deliberately: voice, off-site backup automation, any web UI.
