#!/usr/bin/env bash
set -euo pipefail

# Writes /etc/opencode-server.env on LXC 105 with mode 0600, prompting for
# the password without echoing it back. Mirrors deploy/set-secrets.sh for
# the homelab-agent: the prompt happens through `pct exec` over the
# interactive shell `pct enter 105` already opens, nothing gets typed over
# SSH into a logged command, and the env file is only ever written through
# a 0600 temp file that's renamed into place.

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root, inside LXC 105 (start with 'pct enter 105' on the host)." >&2
  exit 1
fi

if [ ! -x /root/.opencode/bin/opencode ]; then
  echo "opencode is not installed at /root/.opencode/bin/opencode." >&2
  echo "Run 'bash /opt/homelab-agent/deploy/create-lxc-105.sh' from the host first." >&2
  exit 1
fi

read -r -p "OPENCODE_SERVER_PASSWORD (leave blank to auto-generate): " INPUT_PASSWORD
echo

if [ -z "${INPUT_PASSWORD:-}" ]; then
  GENERATED="$(openssl rand -hex 32)"
  PASSWORD="$GENERATED"
  echo "Generated a 64-char hex password. Copy this from the machine by hand - the script will not echo it again:"
  echo "  /root/.opencode-homelab.password"
else
  PASSWORD="$INPUT_PASSWORD"
fi

UMASK_ORIG="$(umask)"
umask 077
TMP="$(mktemp)"
trap 'rm -f "$TMP"; umask "$UMASK_ORIG"' EXIT
printf 'OPENCODE_SERVER_PASSWORD=%s\n' "$PASSWORD" > "$TMP"
install -m 0600 "$TMP" /etc/opencode-server.env

printf '%s' "$PASSWORD" > /root/.opencode-homelab.password
chmod 0600 /root/.opencode-homelab.password

# Show fingerprint, not the value, so a shoulder-surfer looking at this
# terminal at the wrong moment still cannot reach in.
HASH="$(printf '%s' "$PASSWORD" | sha256sum | awk '{print $1}')"
echo "Wrote /etc/opencode-server.env and /root/.opencode-homelab.password (mode 0600)."
echo "Password SHA-256 (for sanity checks, not the password itself): $HASH"
echo "Next: enable the unit: systemctl daemon-reload && systemctl enable --now opencode && systemctl is-active opencode"
