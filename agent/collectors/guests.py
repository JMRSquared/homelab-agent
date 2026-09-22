"""Proxmox guest states: id -> status. Exact comparison, no banding.

A guest's status is a small closed set of strings that changes only when
something genuinely started or stopped, so there is nothing here to band.
"""

from typing import Any

from agent.collectors.registry import Collector, register, safe
from agent.tools import infra

KEY = "guests"


def collect() -> dict[str, Any]:
    guests = safe(infra.guests_list)
    return {
        KEY: (
            {str(g["id"]): g["status"] for g in guests["guests"]}
            if "guests" in guests
            else guests
        )
    }


COLLECTOR = register(Collector(name="guests", keys=(KEY,), collect=collect))
