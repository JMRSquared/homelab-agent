"""Send a local image to the model and return what it sees.

Built to close a specific gap: `photos_download` picks Immich's top
similarity match, and a similarity match is not the same thing as the right
subject - a black Audi S3 and RS3 are visually near-identical to a
CLIP-style embedding model, and the search ranked the S3 first for a query
asking for the RS3. No prompt wording fixes that; the agent has to actually
look at the downloaded file. MiniMax-M3 on the owner's Token Plan has
native image understanding in the same quota as everything else the agent
already does, so this tool is the one missing piece: a way to point the
model at a specific file and ask it a specific question.

Client choice: this builds a plain, synchronous `OpenAI` client from the
same env vars `agent/config.py` uses (see `agent.clients.minimax_client`),
rather than importing `agent.model.Agent` and reusing its client. `Agent`
is async, holds per-conversation state (the audit callback, the message
history it's mid-building), and reusing its instance from inside a tool
call it might itself be executing is exactly the kind of loop this needs
to avoid. A tool-local client is one extra object, not a shared one, and
never touches `Agent` at all.
"""

import base64
from io import BytesIO
from typing import Any

from PIL import Image

from agent import clients
from agent.tools import imaging, outbox
from agent.tools.base import tool

# MiniMax's endpoint (like most OpenAI-compatible vision APIs) accepts an
# image as a data: URL inline in the message content, verified against the
# same request shape agent/model.py's tool-calling loop already proves live
# in deploy/test-minimax.sh. See deploy/test-minimax-vision.sh, added
# alongside this tool, for a live-endpoint check of this specific shape -
# it was not possible to run that script against the real MiniMax account
# from this environment (no network path to it, no API key here), so this
# is the documented OpenAI-compatible shape, not yet confirmed against the
# live endpoint. Run that script before relying on this in production.
MAX_TOKENS = 1024

# Anthropic's and OpenAI's own vision guidance converge on ~1568px as the
# point past which a longer edge stops improving model accuracy and only
# costs more tokens and request size - used here as a reasonable, testable
# default rather than a MiniMax-specific number (nothing in MiniMax's public
# docs was available to check against from this environment either).
MAX_DIMENSION = 1568

# Below this raw size, send the file as-is - decoding and re-encoding a
# small image just to downscale it further buys nothing. Above it, a
# picture is either high-resolution (worth shrinking) or already-compressed
# in a way re-encoding can still usually shrink further (the MT5 screendump
# path: PPM-derived PNGs compress far better as JPEG). 1.5MB keeps the
# base64'd request body under MiniMax's likely per-request size ceiling
# with real margin (base64 adds ~33%; 1.5MB raw is ~2MB encoded).
DOWNSCALE_ABOVE_BYTES = 1_500_000

JPEG_QUALITY = 85


def _prepare_for_upload(data: bytes, content_type: str) -> tuple[bytes, str]:
    """Return (bytes, content_type) ready to base64-encode and send.

    Small images pass through untouched. Anything bigger is decoded with
    Pillow, shrunk to fit within MAX_DIMENSION on its longest edge, and
    re-encoded as JPEG - chosen over re-encoding in the original format
    because it's universally supported or by every vision endpoint,
    unlike e.g. TIFF, and compresses far better than PNG for a photo.
    """
    if len(data) <= DOWNSCALE_ABOVE_BYTES:
        return data, content_type
    try:
        with Image.open(BytesIO(data)) as opened:
            rgb = opened.convert("RGB")
            rgb.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
            buf = BytesIO()
            rgb.save(buf, format="JPEG", quality=JPEG_QUALITY)
            return buf.getvalue(), "image/jpeg"
    except Exception as exc:
        raise ValueError(
            f"could not decode the image to downscale it ({type(exc).__name__}: {exc}) - "
            "is it actually an image file?"
        ) from exc


@tool(
    "image_inspect",
    "Look at a local image file and answer a question about what's in it, using the "
    "model's own vision. Use this to VERIFY a downloaded photo or screenshot actually "
    "shows what it's supposed to before attaching or reporting it as correct - "
    "photos_download's result is a similarity match, not a confirmed one, and a wrong "
    "but confident-looking attachment is worse than admitting uncertainty. `path` must "
    "already exist inside the agent's outbox (a path returned by photos_download, "
    "mt5_screenshot, or similar), not an arbitrary filesystem path. `question` should be "
    "specific (e.g. \"does this car have an RS3 badge and honeycomb grille, or something "
    "else?\") rather than a generic \"describe this image\".",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "question": {"type": "string", "minLength": 1},
        },
        "required": ["path", "question"],
        "additionalProperties": False,
    },
)
def image_inspect(path: str, question: str) -> dict[str, Any]:
    question = question.strip()
    if not question:
        raise ValueError("question must not be blank")

    image_path = outbox.resolve_in_outbox(path)
    raw = image_path.read_bytes()
    content_type = imaging.sniff_content_type(raw, image_path.name)
    if not content_type.startswith("image/"):
        raise ValueError(f"{path!r} doesn't look like an image (sniffed as {content_type!r})")

    data, content_type = _prepare_for_upload(raw, content_type)
    data_url = f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"

    client = clients.minimax_client()
    try:
        response = client.chat.completions.create(
            model=clients.minimax_model(),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            max_tokens=MAX_TOKENS,
        )
    except Exception as exc:
        raise RuntimeError(
            f"the model endpoint rejected the image or request: {type(exc).__name__}: {exc}"
        ) from exc

    choices = response.choices
    answer = (choices[0].message.content or "").strip() if choices else ""
    return {
        "path": str(image_path),
        "question": question,
        "answer": answer,
        "downscaled": len(raw) > DOWNSCALE_ABOVE_BYTES,
    }
