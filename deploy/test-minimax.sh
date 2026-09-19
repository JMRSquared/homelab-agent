#!/usr/bin/env bash
#
# Check that a MiniMax API key works, before wiring up anything else.
#
# Run this INSIDE the agent container:
#     bash /opt/homelab-agent/deploy/test-minimax.sh
#
# It prompts for the key without echoing it, sends one tiny request, and tells
# you whether the key, the model name and the network path all work. It writes
# nothing and changes nothing — safe to run as many times as you like.
#
# If /etc/homelab-agent/env already has a key, it offers to reuse it so you are
# not re-pasting a token you already stored.

set -euo pipefail

VENV_PY=/opt/homelab-agent/.venv/bin/python
ENV_FILE=/etc/homelab-agent/env
BASE_URL=${MINIMAX_BASE_URL:-https://api.minimax.io/v1}
MODEL=${MINIMAX_MODEL:-MiniMax-M3}

if [ ! -x "$VENV_PY" ]; then
  echo "No virtualenv at ${VENV_PY}." >&2
  echo "Run the bootstrap in docs/deploy.md first." >&2
  exit 1
fi

KEY=""
if [ -f "$ENV_FILE" ]; then
  KEY=$(awk -F= '/^MINIMAX_API_KEY=/{print substr($0, index($0,"=")+1)}' "$ENV_FILE" 2>/dev/null || true)
  if [ -n "$KEY" ]; then
    printf 'Found a key in %s (%s…). Use it? [Y/n]: ' "$ENV_FILE" "$(printf '%s' "$KEY" | cut -c1-4)"
    read -r reply
    case "$reply" in
      [Nn]*) KEY="" ;;
    esac
  fi
fi

if [ -z "$KEY" ]; then
  read -r -s -p "Paste your MiniMax API key: " KEY
  echo
fi

if [ -z "$KEY" ]; then
  echo "No key given." >&2
  exit 1
fi

echo
echo "Endpoint : ${BASE_URL}"
echo "Model    : ${MODEL}"
echo "Sending one request…"
echo

MINIMAX_API_KEY="$KEY" MINIMAX_BASE_URL="$BASE_URL" MINIMAX_MODEL="$MODEL" \
"$VENV_PY" - <<'PY'
import os
import sys

from openai import OpenAI

client = OpenAI(
    api_key=os.environ["MINIMAX_API_KEY"],
    base_url=os.environ["MINIMAX_BASE_URL"],
)

try:
    response = client.chat.completions.create(
        model=os.environ["MINIMAX_MODEL"],
        messages=[
            {
                "role": "user",
                "content": "Reply with exactly: homelab agent key works",
            }
        ],
        max_tokens=32,
    )
except Exception as exc:  # noqa: BLE001 - this script's whole job is reporting the failure
    name = type(exc).__name__
    print(f"FAILED — {name}: {exc}\n")
    text = str(exc).lower()
    if "401" in text or "unauthor" in text or "invalid api key" in text:
        print("That key was rejected. Check you copied the whole value from the")
        print("MiniMax platform, and that it is a Token Plan subscription key.")
    elif "404" in text or "model" in text:
        print(f"The endpoint did not recognise model {os.environ['MINIMAX_MODEL']!r}.")
        print("Check which models your plan exposes and set MINIMAX_MODEL to match.")
    elif "connect" in text or "timeout" in text or "resolve" in text:
        print("Could not reach the endpoint at all. Check the container has DNS")
        print("and outbound HTTPS: try `getent hosts api.minimax.io` inside it.")
    sys.exit(1)

reply = (response.choices[0].message.content or "").strip()
usage = response.usage

print("SUCCESS — the key, the model and the network path all work.")
print(f"  model replied : {reply!r}")
if usage is not None:
    print(f"  tokens used   : {usage.prompt_tokens} in, {usage.completion_tokens} out")
PY

echo
echo "Next: run deploy/set-secrets.sh to store this key along with the other"
echo "values the agent needs, then start the service."
