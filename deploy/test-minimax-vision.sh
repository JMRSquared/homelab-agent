#!/usr/bin/env bash
#
# Check that MiniMax-M3's image understanding actually works over the
# OpenAI-compatible chat completions endpoint, on this account's plan.
#
# Run this INSIDE the agent container:
#     bash /opt/homelab-agent/deploy/test-minimax-vision.sh
#
# Companion to deploy/test-minimax.sh, which proves the tool-calling wire
# contract. This proves the other unproven integration point agent/tools/
# vision.py depends on: that this MiniMax endpoint accepts an image as an
# inline `data:` URL in a `content` list the way OpenAI's own API does.
# That shape was NOT verified against the live endpoint when image_inspect
# was built - there was no network path to a real MiniMax account or API
# key from that environment - so this script is the thing that actually
# proves it, the same way test-minimax.sh's own comments describe for tool
# calling: getting a plain reply proves the key and model name work, not
# that a specific request shape is accepted, and a compatibility endpoint
# can easily accept the general shape of a request while rejecting a
# specific field within it.
#
# It sends one tiny (16x16, solid colour) PNG built on the fly, so it
# proves the request shape without needing a real photo on disk. It writes
# nothing and changes nothing - safe to run as many times as you like.

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
echo "Sending one request with an inline image…"
echo

MINIMAX_API_KEY="$KEY" MINIMAX_BASE_URL="$BASE_URL" MINIMAX_MODEL="$MODEL" \
"$VENV_PY" - <<'PY'
import base64
import os
import sys
import zlib


def _tiny_red_png() -> bytes:
    """A minimal, valid 16x16 solid-red PNG, built by hand so this script
    needs no dependency (not even Pillow) beyond the standard library."""
    width = height = 16
    raw = b""
    for _ in range(height):
        raw += b"\x00" + b"\xff\x00\x00" * width  # filter byte 0, then RGB pixels

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            len(data).to_bytes(4, "big")
            + tag
            + data
            + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big")
        )

    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


from openai import OpenAI  # noqa: E402

client = OpenAI(
    api_key=os.environ["MINIMAX_API_KEY"],
    base_url=os.environ["MINIMAX_BASE_URL"],
)
model = os.environ["MINIMAX_MODEL"]

data_url = "data:image/png;base64," + base64.b64encode(_tiny_red_png()).decode("ascii")

# The exact shape agent/tools/vision.py's image_inspect sends: a `content`
# list with a text part and an `image_url` part carrying a data: URL.
try:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What color is this image? Answer in one word."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        max_tokens=64,
    )
except Exception as exc:  # noqa: BLE001 - this script's whole job is reporting the failure
    name = type(exc).__name__
    print(f"FAILED — {name}: {exc}\n")
    text = str(exc).lower()
    if "400" in text or "invalid" in text or "unsupported" in text:
        print("The endpoint rejected the image_url content-part shape. Check MiniMax's")
        print(f"own API docs for {model!r} - it may want a different field name, a")
        print("different data: URL form, or a separate vision-specific endpoint/model")
        print("rather than plain chat completions. agent/tools/vision.py's")
        print("image_inspect will need updating to match whatever shape IS accepted.")
    elif "401" in text or "unauthor" in text:
        print("That key was rejected - same as test-minimax.sh's check, unrelated to")
        print("the image shape itself.")
    sys.exit(1)

reply = (response.choices[0].message.content or "").strip()
usage = response.usage

print("SUCCESS — MiniMax accepted an inline image_url content part.")
print(f"  model replied : {reply!r}")
if "red" not in reply.lower():
    print("  NOTE: the reply doesn't mention \"red\" - the request shape was accepted")
    print("  (no error), but check the model is actually looking at the image rather")
    print("  than answering blind.")
if usage is not None:
    print(f"  tokens used   : {usage.prompt_tokens} in, {usage.completion_tokens} out")
PY

echo
echo "If this failed, agent/tools/vision.py's image_inspect needs its request"
echo "shape updated to match whatever this printed - it has not been confirmed"
echo "against a live MiniMax account."
