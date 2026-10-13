# Checkout guard

## Problem

On 2026-10-11 at 07:28:57 UTC the improve cycle tried to clear `__pycache__`
folders in its own checkout with:

    cd /opt/homelab-agent && find . -name '__pycache__' -path './.venv/*' -prune \
      -o -name '__pycache__' -path './agent/*' -prune -o -print | xargs rm -rf

The trailing `-o -print` matched every file. `xargs rm -rf` deleted the source,
`.git` and `.venv` of the running agent. The process kept running from memory,
but every new HTTPS client failed (certifi's CA file was gone), so Slack and every
hostctl-routed tool returned `FileNotFoundError` for two days. The agent then
misdiagnosed it as a hostctl outage (incident #30). Recovery needed the owner and
a vzdump backup.

Two gaps let this happen:

1. `guest_exec` and `host_exec` pass any command to hostctl. Nothing stops the
   model from deleting the checkout it runs from.
2. hostctl's `log_requests` middleware logs each request body to the
   `hostctl.access` logger, but that logger stays at the default WARNING level,
   so the journal only had uvicorn's access line. The destructive command could
   only be found in the agent's own database.

## Solution

1. `guest_exec`, `host_exec` and `job_start` check each command before calling hostctl. A
   recursive delete aimed at the agent's checkout is refused with a `ValueError`
   that says why and what to use instead. No request reaches hostctl.
2. The `hostctl.access` logger logs at INFO to stderr, so systemd's journal
   records method, path, client, body and status for every request.

## User stories

1. As the owner, I want the agent unable to delete its own checkout through its
   shell tools, so a bad cleanup command cannot take the agent down.
2. As the agent, I want a refused command to tell me why and what to do instead,
   so I do not retry the same mistake.
3. As the agent, I want ordinary commands in my checkout (reading files, git,
   pytest, ruff) to keep working, so the guard does not block my real work.
4. As the owner, I want hostctl's journal to show what each request asked for,
   so I can see what was run on the host and guests without the agent's database.

## Implementation decisions

- The guard sits in `agent/tools/infra.py`, in front of the hostctl call in both
  `guest_exec` and `host_exec`. `agent/tick.py` calls tool functions directly,
  so the check must live in those functions, not in the tool schema layer.
- A command is refused when it contains a recursive delete and a target that can
  reach the checkout. Recursive deletes: `rm` with `-r`, `-R`, `--recursive` or
  combined flags such as `-rf`/`-fr`; `xargs ... rm`; `find ... -delete`;
  `find ... -exec rm`; `git clean`; `shutil.rmtree`. Targets: the string
  `homelab-agent` (covers `/opt/homelab-agent`, `cd` into it, and `.venv` paths
  under it); `/opt` followed by a glob or as the delete root; and on guest 104,
  a bare `*`, `.` or `./*` delete root. Matching is on the raw command string.
  False positives are acceptable; false negatives on the incident command are not.
- No allow-list. The agent never needs to delete inside its checkout: `python -B`
  or `PYTHONDONTWRITEBYTECODE=1` avoids bytecode, and `self_deploy` resets the
  tree. The refusal message names those.
- The refusal is a `ValueError`, the same error type the other infra tools use for
  bad input, so `tools/base.dispatch` reports it as a normal tool error.
- hostctl: give `hostctl.access` level INFO and one `StreamHandler` at import,
  guarded so a re-import does not add a second handler. Keep never logging the
  Authorization header.

## Testing decisions

- Seams: `agent.tools.infra.guest_exec` / `host_exec` with `respx` (existing
  pattern in `tests/agent/test_infra_tools.py`), asserting the hostctl route was
  never called for refused commands and was called for allowed ones.
- hostctl: `fastapi.testclient.TestClient(app)` with an authenticated request and
  pytest's `caplog` (or a handler check) on `hostctl.access`.

## Acceptance scenarios

- GRD-001 guest_exec on guest 104 refuses `rm -rf /opt/homelab-agent` without calling hostctl
- GRD-002 guest_exec refuses the exact 2026-10-11 `find ... -o -print | xargs rm -rf` command run in /opt/homelab-agent
- GRD-003 host_exec refuses `find`/`git clean`/`rm -r` deletes that target homelab-agent or a glob under /opt
- GRD-004 the refusal message names the checkout and suggests python -B, PYTHONDONTWRITEBYTECODE or self_deploy
- GRD-005 guest_exec still runs non-destructive commands in the checkout (cat, git status, pytest) through hostctl
- GRD-006 hostctl logs method, path, client, body and status at INFO for an authenticated request
- GRD-007 hostctl's request log never contains the Authorization header value
- GRD-009 the guard decides a 40-option rsync and a 2000-token git command in well under a second
- GRD-008 job_start refuses the 2026-10-11 command as a background job without calling hostctl, and still starts ordinary jobs

## Out of scope

- Restricting the improve cycle further (running as another user, read-only checkout).
- Guarding other destructive commands (`zfs destroy`, `pct destroy`); the troubleshooting skill already forbids them.
- Correcting incident #30 in the agent's memory (ops task after deploy).
- Turning `/opt/hostctl` into a git checkout (docs/deploy.md section 1b).
