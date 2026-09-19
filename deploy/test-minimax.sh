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
# It then proves the actual thing the agent depends on: OpenAI-shaped tool
# calling on this endpoint. Getting a plain reply back (the check above)
# does not prove MiniMax-M3 supports `tools`/`tool_calls`, or that appending
# `msg.model_dump(exclude_none=True)` — exactly what agent/model.py does —
# round-trips on a second turn. A compatibility endpoint can easily accept
# a request shaped like OpenAI's and still reject fields the OpenAI SDK
# dumps back out. This is the single highest-value unproven integration
# point: nothing in the tool loop works if it is wrong, so it is checked
# here with a real request, not a mock.
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
echo "Checking the tool-calling wire contract (this is what the agent actually"
echo "uses, not just a plain reply)…"
echo

MINIMAX_API_KEY="$KEY" MINIMAX_BASE_URL="$BASE_URL" MINIMAX_MODEL="$MODEL" \
"$VENV_PY" - <<'PY'
import json
import os
import sys

from openai import OpenAI

client = OpenAI(
    api_key=os.environ["MINIMAX_API_KEY"],
    base_url=os.environ["MINIMAX_BASE_URL"],
)
model = os.environ["MINIMAX_MODEL"]

# One trivial tool definition, shaped exactly like agent/tools/base.py's
# openai_schema() output.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ping",
            "description": "Reply with pong. Call this whenever asked to ping.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }
]

messages: list[dict] = [
    {"role": "user", "content": "Call the ping tool now, then nothing else."}
]

# --- Half 1: does the model return tool_calls at all? ---
try:
    first = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=TOOLS,
        tool_choice="required",
    )
except Exception as exc:  # noqa: BLE001
    print(f"FAILED (half 1/2: requesting a tool call) — {type(exc).__name__}: {exc}\n")
    print("The endpoint rejected a request carrying `tools`/`tool_choice`. Either")
    print(f"{model!r} doesn't support OpenAI-shaped tool calling on this endpoint, or")
    print("`tool_choice=\"required\"` specifically isn't supported — check MiniMax's")
    print("docs for this model's tool-calling support before relying on it.")
    sys.exit(1)

msg = first.choices[0].message
if not msg.tool_calls:
    print("FAILED (half 1/2: requesting a tool call) — no tool_calls in the response.\n")
    print(f"  finish_reason : {first.choices[0].finish_reason!r}")
    print(f"  content       : {msg.content!r}")
    print(f"{model!r} answered but did not call the tool even with tool_choice=\"required\".")
    sys.exit(1)

print("PASSED (half 1/2) — the model returned a tool call.")
call = msg.tool_calls[0]
print(f"  tool called   : {call.function.name!r}")
print(f"  arguments     : {call.function.arguments!r}")

# --- Half 2: does appending msg.model_dump(exclude_none=True) — exactly
# what agent/model.py:48 does — plus a tool result round-trip on the next
# turn? A compatibility endpoint can accept an OpenAI-shaped *request* and
# still reject fields the OpenAI SDK's own model_dump() serialises back out.
messages.append(msg.model_dump(exclude_none=True))
messages.append(
    {
        "role": "tool",
        "tool_call_id": call.id,
        "content": json.dumps({"ok": True, "result": "pong"}),
    }
)

try:
    second = client.chat.completions.create(model=model, messages=messages, tools=TOOLS)
except Exception as exc:  # noqa: BLE001
    print(f"\nFAILED (half 2/2: the follow-up turn) — {type(exc).__name__}: {exc}\n")
    print("The first turn worked, but appending the assistant message exactly as")
    print("agent/model.py does and sending the tool result back was rejected. This")
    print("is the exact wire shape the real agent loop depends on every tool call.")
    sys.exit(1)

reply = (second.choices[0].message.content or "").strip()
print("PASSED (half 2/2) — the follow-up turn with the appended tool result was accepted.")
print(f"  model replied : {reply!r}")
print()
print("SUCCESS — MiniMax-M3 supports OpenAI-shaped tool calling on this endpoint,")
print("and the exact append/round-trip shape agent/model.py uses works.")
PY

echo
echo "Next: run deploy/set-secrets.sh to store this key along with the other"
echo "values the agent needs, then start the service."
