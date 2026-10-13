# Decisions

| Date | Decision | Why |
|---|---|---|
| 2026-10-13 | Pushed the 13 recovered commits (5390e7d..d9f47da) to GitHub main as a fast-forward before branching | The deployed code existed only in LXC 104 and its vzdump backups; the milestone must build on what runs, and the push gives the code an off-site copy |
| 2026-10-13 | Guard lives in agent/tools/infra.py (guest_exec, host_exec), checked before the hostctl call | Every shell the model gets passes through those two functions; hostctl stays a dumb executor and needs no deploy for the guard |
| 2026-10-13 | Refuse any recursive delete in a command that names the checkout, its parent /opt with a glob, or runs on guest 104 against a bare `*`/`.` target, with no allow-list for __pycache__ cleanup | The incident command was itself a "narrow" __pycache__ cleanup; the bot never needs to delete inside its checkout (python -B or PYTHONDONTWRITEBYTECODE avoids bytecode, self_deploy resets the tree) |
| 2026-10-13 | hostctl access logger set to INFO with its own stream handler at import | uvicorn configures only its own loggers, so `hostctl.access` inherited WARNING and every request body was dropped; the incident's command could not be read from the journal |
| 2026-10-13 | Two tickets, no shared serial | The guard and the logging touch disjoint files and can build at once |
| 2026-10-13 | Deploy: pull + reinstall + restart in LXC 104, rsync hostctl/ to /opt/hostctl/hostctl/ + restart, then re-enable homelab-improve.timer | Matches docs/deploy.md; /opt/hostctl is still a copied folder, not a checkout |
| 2026-10-13 | Bot memory correction for incident #30 is an ops task after deploy, not a ticket | It is data in agent.db and brain.md, not code |
| 2026-10-13 | Added T03: job_start gets the same guard | T01's builder found job_start sends raw shell commands to hostctl unguarded; without it the agent could still run the incident command, so user story 1 is not met |

## Outcomes

- [ ] guest_exec and host_exec refuse a recursive delete aimed at the agent's own
      checkout before anything reaches hostctl, and say what to do instead.
- [ ] The exact 2026-10-11 command is refused.
- [ ] Ordinary commands in the checkout (reading files, git status, pytest) still run.
- [ ] hostctl's journal records method, path, client and body of every request,
      never the Authorization header.
- [ ] Both changes are deployed (agent in LXC 104, hostctl on the host) and the
      homelab-improve timer is back on.
