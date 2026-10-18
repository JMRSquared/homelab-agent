#!/usr/bin/env bash
set -euo pipefail

# OpenCode's own container. VMID 105 / 10.0.0.169 continues the assignment
# pattern from create-lxc-104.sh (103→10.0.0.167 was already taken by the
# mail container; 104→10.0.0.168 took the next slot; 105→10.0.0.169 takes
# the one after that). Verified free by the same reasoning as 104 was -
# `pct create 105` would fail loudly if 105 were already used, and 169 is
# outside the four IPs already assigned (165/166/167/168). Do not renumber
# back into the existing LXC range.

# Same template resolution as create-lxc-104.sh: pick the newest local
# Debian 12 template rather than pinning a version that would go stale.
TEMPLATE=$(pveam list local | awk '{print $1}' | grep 'debian-12-standard' | sort -V | tail -n1)
if [ -z "$TEMPLATE" ]; then
  echo "No debian-12-standard template found in 'pveam list local'." >&2
  echo "Fix: pveam update && pveam available | grep debian-12-standard" >&2
  echo "Then: pveam download local <template-name-from-that-list>" >&2
  exit 1
fi
echo "Using template: $TEMPLATE"

# Sizing: 2 cores / 2 GB RAM matches the agent LXC's footprint, which is
# what Bun + an OpenCode session actually use on a normal workload.
# 16 GB disk is more than enough - opencode keeps its sqlite db and checkouts
# under the opencode user's own ~/.local/share/opencode, never on a separate
# dataset - so no ZFS mount, no idmap dance, no /tank/dev/opencode directory
# to create. Keeping the LXC self-contained means an `apt upgrade && reboot`
# is the whole operational surface, with nothing on /tank to remember about.
pct create 105 "$TEMPLATE" \
  --hostname opencode \
  --cores 2 --memory 2048 --swap 512 \
  --rootfs local-lvm:16 \
  --net0 name=eth0,bridge=vmbr0,ip=10.0.0.169/24,gw=10.0.0.254 \
  --features nesting=1 \
  --onboot 1 --unprivileged 1

pct start 105
# OpenCode only needs curl for the official installer and git for plugin
# checkouts - the Bun runtime is bundled into the single static binary the
# installer drops at /root/.opencode/bin/opencode, so no Node, no Python,
# no separate package manager venv. The "Install OpenCode" line below is
# idempotent; the installer itself recognises the existing install path
# and exits cleanly rather than re-downloading.
pct exec 105 -- apt-get update
pct exec 105 -- apt-get install -y curl git ca-certificates
pct exec 105 -- bash -lc 'curl -fsSL https://opencode.ai/install | bash'
# The official installer defaults to OpenCode 1.x as of October 2026;
# v2 is required for the OPENCODE_SERVER_PASSWORD auth path this
# deployment relies on (v1 has no built-in auth on the serve endpoint,
# so the LXC would happily serve every connected Tailscale client with
# no password at all). `opencode upgrade --major` reads "OpenCode 2 is
# available" and stops at v1 unless this flag is present. Re-running is
# a no-op once v2 is installed.
pct exec 105 -- bash -lc 'opencode upgrade --major 2>&1 | tail -3'
pct exec 105 -- /root/.opencode/bin/opencode --version
