#!/usr/bin/env bash
set -euo pipefail

# VMID 103 and 10.0.0.167 look like the obvious next-available choices, but
# both are already taken on the live host: LXC 103 is the existing "mail"
# container, already running at 10.0.0.167/24. `pct create 103` would just
# fail (so nothing would be destroyed by trying), but reusing 103's IP on a
# different VMID would fight the mail server for that address on the
# network. Verified free by probing the host directly: VMID 104 and
# 10.0.0.168. Do not renumber this back to 103.

# Resolve the newest local Debian 12 template instead of pinning a point
# version: `pveam list local`'s first column is the volume id
# (local:vztmpl/debian-12-standard_<version>_amd64.tar.zst); filter to
# debian-12-standard, then `sort -V` so "12.12-1" correctly sorts after
# "12.7-1" (a plain lexicographic sort would get that backwards), and take
# the last line. Pinning a version here just moves this same failure to the
# next time the user refreshes templates on the host.
TEMPLATE=$(pveam list local | awk '{print $1}' | grep 'debian-12-standard' | sort -V | tail -n1)
if [ -z "$TEMPLATE" ]; then
  echo "No debian-12-standard template found in 'pveam list local'." >&2
  echo "Fix: pveam update && pveam available | grep debian-12-standard" >&2
  echo "Then: pveam download local <template-name-from-that-list>" >&2
  exit 1
fi
echo "Using template: $TEMPLATE"

pct create 104 "$TEMPLATE" \
  --hostname agent \
  --cores 2 --memory 2048 --swap 512 \
  --rootfs local-lvm:16 \
  --net0 name=eth0,bridge=vmbr0,ip=10.0.0.168/24,gw=10.0.0.254 \
  --features nesting=1 \
  --onboot 1 --unprivileged 1

pct set 104 -mp0 /tank/dev/agent,mp=/tank/dev/agent
mkdir -p /tank/dev/agent
# Container is --unprivileged 1, so Proxmox's default idmap maps container
# uid 0 to host uid 100000. A host-root-owned directory falls outside that
# range and shows up as the overflow/nobody uid inside the container, so
# the agent could not create its own sqlite db or brain file. Do not remove.
chown -R 100000:100000 /tank/dev/agent
pct start 104
pct exec 104 -- apt-get update
pct exec 104 -- apt-get install -y python3 python3-venv python3-pip git
