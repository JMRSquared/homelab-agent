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
pct exec 103 -- apt-get install -y python3 python3-venv python3-pip git openssh-client

# The agent's code is deployed by cloning the private GitHub repo directly onto
# this container (see docs/deploy.md) rather than pushing a tarball from a
# laptop, so the container needs its own SSH identity for GitHub and needs to
# already trust github.com's host key before the first `git clone` runs.
pct exec 103 -- mkdir -p /root/.ssh
pct exec 103 -- chmod 700 /root/.ssh
pct exec 103 -- ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ""
pct exec 103 -- sh -c 'ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts'
pct exec 103 -- chmod 600 /root/.ssh/known_hosts

echo
echo "=============================================================="
echo "LXC 103 deploy key (public) - add this as a READ-ONLY deploy"
echo "key on the GitHub repo before running the clone step in"
echo "docs/deploy.md. Do not add write access; the container only"
echo "ever needs to pull."
echo "=============================================================="
pct exec 103 -- cat /root/.ssh/id_ed25519.pub
echo "=============================================================="
