# OpenCode server setup

How to take a fresh Proxmox host at 10.0.0.2 from "no OpenCode server" to
"a long-running OpenCode server on LXC 105 that the Mac at 10.0.0.190 (and
anywhere on Tailscale) can reach". Run these steps in order, on a machine
with SSH access to `root@10.0.0.2`, confirming each step's expected output
before moving to the next. Nothing here runs itself.

The homelab-agent already runs on LXC 104 at 10.0.0.168 following the same
pattern (see `docs/deploy.md`). OpenCode on LXC 105 is its sibling, not a
replacement.

Tailscale on the host (`100.102.145.0`) advertises `10.0.0.0/24`, so the
Mac reaches LXC 105 at `10.0.0.169` whether it is on the LAN or coming in
through Tailscale from outside. There is no port forwarding, and
`OPENCODE_SERVER_PASSWORD` (Basic auth on the OpenCode HTTP API) plus the
LXC's own bind to `10.0.0.169` only are the security boundary.

## 1. Create LXC 105

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'bash -s' < deploy/create-lxc-105.sh
```

Expected last line: `opencode v2.0.32`. The create script installs v1
first (whatever `https://opencode.ai/install` picks as default) and
then runs `opencode upgrade --major` to bump it to v2 - v2 is the only
branch where `OPENCODE_SERVER_PASSWORD` Basic auth works, and the whole
password/session setup below relies on it. If `pct create 105` reports
the VMID as already used, do not renumber back into the existing LXC
range - see the comment in `deploy/create-lxc-105.sh` for what was free
as of writing.

## 2. Push the deploy files into LXC 105

LXC 105 is its own world at this point - it has opencode, but none of
the homelab-agent `deploy/` files yet. Push the three we need across
using the same `scp` + `pct push` pattern `docs/deploy.md` step 8 uses
for the homelab-agent's own service file:

```bash
scp deploy/opencode.service deploy/opencode-env.example deploy/set-opencode-secrets.sh \
  root@10.0.0.2:/tmp/
ssh -n -o BatchMode=yes root@10.0.0.2 '
  pct push 105 /tmp/opencode.service       /etc/systemd/system/opencode.service
  pct push 105 /tmp/opencode-env.example   /etc/opencode-server.env
  pct push 105 /tmp/set-opencode-secrets.sh /root/set-opencode-secrets.sh
  chmod 0600 /etc/opencode-server.env
  chmod 0700 /root/set-opencode-secrets.sh
'
```

The env file is pushed in at `0600` *with the password line still blank*
so step 3's prompt can fill it in - the rotation script then overwrites
it through a `0600` temp file + rename, so there is never a moment where
a real password sits at a looser mode. `set-opencode-secrets.sh` runs
from `/root/` rather than from a clone of this repo: LXC 105 does not
own its own checkout of `homelab-agent` (only LXC 104 needs one for its
own deploy loop), so this `pct push` IS the install path. To bring any
of the three files back in sync after editing locally, repeat the
matching `pct push` line.

## 3. Set the server password on LXC 105

Enter the LXC as root (this is the only step that drops to an interactive
shell, for the same reason `set-secrets.sh` does - the password should
never travel through SSH command history):

```bash
ssh -t -o BatchMode=no root@10.0.0.2 'pct enter 105'
```

Then, inside the LXC's root shell:

```bash
/root/set-opencode-secrets.sh
```

Leaving the prompt blank auto-generates a 64-character hex password and
writes it twice: `OPENCODE_SERVER_PASSWORD=<value>` to
`/etc/opencode-server.env` (mode 0600) and the bare value to
`/root/.opencode-homelab.password` (mode 0600). The second file is the
one to copy back to the Mac in step 5 - never paste the password through
SSH command lines. The script prints a `sha256sum` fingerprint of the
value it just wrote so you can compare fingerprints across machines
without ever displaying the password itself.

Re-run this script later to rotate the password: the env file gets
overwritten in place, with the same 0600 temp-file + rename atomicity as
the first run. Exit the container shell (`exit` or Ctrl-D) once it
reports success.

## 4. Install the systemd unit and start the server

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 105 -- systemctl daemon-reload'
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 105 -- systemctl enable --now opencode'
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 105 -- systemctl is-active opencode'
```

Expected: `active`. Confirm it is listening on the right address (and
nothing else):

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 105 -- ss -ltnp | grep 4096'
```

Expected: a line listing `opencode` as the user/PID with local address
`10.0.0.169:4096` (or similar - the process name column will say
`opencode`/`serve`, not `uvicorn` or `bun`). If the bind shows `*:4096`
or `0.0.0.0:4096`, the `--hostname 10.0.0.169` flag in
`deploy/opencode.service` did not take effect - recheck that line in
the ExecStart.

## 5. Pull the password back to the Mac

The password file lives inside LXC 105, not on the host's filesystem,
so two steps: `pct pull` on the host (the file lives at
`/root/.opencode-homelab.password` inside the container), then `scp`
to the Mac. `ssh` to the host authenticates with the same key `make
deploy` uses against LXC 104:

```bash
ssh -n root@10.0.0.2 'pct pull 105 /root/.opencode-homelab.password /tmp/opencode-homelab.password && chmod 0600 /tmp/opencode-homelab.password'
scp -i ~/.ssh/id_ed25519 root@10.0.0.2:/tmp/opencode-homelab.password ~/.opencode/homelab.password
ssh -n root@10.0.0.2 'rm -f /tmp/opencode-homelab.password'
chmod 600 ~/.opencode/homelab.password
```

The Mac-side `chmod 600` is non-negotiable; the file IS the auth
credential and the `--server` flag on the Mac side reads it directly,
without any other gate.

Add to `~/.zshrc` (or `~/.bashrc`):

```sh
export OPENCODE_PASSWORD="$(cat ~/.opencode/homelab.password 2>/dev/null || true)"
alias oc-homelab='opencode --server http://10.0.0.169:4096'
```

Restart the shell, then verify auth in one shot:

```bash
curl -fsS -u "opencode:$(cat ~/.opencode/homelab.password)" \
  http://10.0.0.169:4096/api/info
```

Expected: a JSON body containing `"version":"2.0.32"` and the running
`pid`. If this hangs or refuses, the bind in step 4 was wrong - the
nftables pattern in `docs/deploy.md` step 1a does **not** apply here,
the LXC's `--hostname 10.0.0.169` bind is the gate, and the LAN
is reachable by every device that needs it. If the body comes back as
`401`, the password file on the Mac and the one on LXC 105 disagree -
re-run `set-opencode-secrets.sh`.

## 6. Live smoke test

```bash
oc-homelab 'echo $HOSTNAME and list /root'
```

Expected: model answer that names `opencode` and lists the LXC's `/root`.

If `oc-homelab` cannot connect at all and step 5's `curl` was fine, the
Mac picked up a stale `OPENCODE_PASSWORD` from before the last rotate -
`source ~/.zshrc` again, or `unset OPENCODE_PASSWORD && export
OPENCODE_PASSWORD="$(cat ~/.opencode/homelab.password)"`.

## 7. What's actually running

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 105 -- systemctl status opencode --no-pager'
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 105 -- journalctl -u opencode -n 50 --no-pager'
```

`journalctl` should show one "server listening on http://10.0.0.169:4096"
line on each restart, no password line (the password is read from the
env file, never logged), and no panics. If you see `EADDRINUSE`, another
process is on 4096 - `pct exec 105 -- lsof -iTCP:4096 -sTCP:LISTEN` will
name it.

## Updating OpenCode

The official installer (`curl -fsSL https://opencode.ai/install | bash`)
is also the upgrade path - it overwrites the binary in place at
`/root/.opencode/bin/opencode` and the systemd unit picks up the new
binary on its next restart:

```bash
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 105 -- bash -lc "curl -fsSL https://opencode.ai/install | bash"'
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 105 -- systemctl restart opencode'
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 105 -- systemctl is-active opencode'
```

No other config moves when the binary changes. The Mac keeps its own
`/Users/lavhe/.opencode/bin/opencode` on its own upgrade track - the two
are independent, by design: a Mac upgrade that broke the homelab's binary
(or vice versa) would be a worse outcome than a few versions of drift
between them, and `opencode --version` against each is the diagnostic
when something feels wrong.
