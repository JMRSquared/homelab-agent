"""Agent-facing wrapper around hostctl's `/mt5/screenshot` route.

The agent's own container has no way to reach `qm monitor` on the Proxmox
host - that only exists through hostctl, the privileged boundary. This
module is the tool-side half; `hostctl/screendump.py` does the actual
capture and PPM-to-PNG conversion on the host.
"""

from typing import Any

from agent.clients import hostctl_post_bytes
from agent.tools import imaging, outbox
from agent.tools.base import tool


@tool(
    "mt5_screenshot",
    "Capture a screenshot of the MT5 trading VM's desktop (guest 200) and save it to the "
    "local outbox, ready to attach to an email or message. Read-only against the VM - it "
    "cannot disturb whatever is open in MetaTrader. Can fail if the host is slow to produce "
    "the capture; this never retries automatically, so treat a failure as a real failure, "
    "not something to immediately try again against the live trading VM.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def mt5_screenshot() -> dict[str, Any]:
    png, content_type = hostctl_post_bytes("/mt5/screenshot")
    dest = outbox.new_artifact_path("mt5-screenshot", ".png")
    dest.write_bytes(png)
    dims = imaging.dimensions(png, content_type)
    return {
        "path": str(dest),
        "content_type": content_type,
        "byte_size": len(png),
        "width": dims[0] if dims else None,
        "height": dims[1] if dims else None,
    }
