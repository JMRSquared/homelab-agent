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
# Container is --unprivileged 1, so Proxmox's default idmap maps container
# uid 0 to host uid 100000. A host-root-owned directory falls outside that
# range and shows up as the overflow/nobody uid inside the container, so
# the agent could not create its own sqlite db or brain file. Do not remove.
chown -R 100000:100000 /tank/dev/agent
pct start 103
pct exec 103 -- apt-get update
pct exec 103 -- apt-get install -y python3 python3-venv python3-pip
