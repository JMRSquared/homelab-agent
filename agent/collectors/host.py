"""Proxmox host metrics: load, memory, ARC, uptime.

The original wake storm lived here. `uptime_s` increases every second and
is dropped outright; `load1`, `mem_used_gb` and `arc_gb` never sit still
and are banded with a full-step deadband against their last *reported*
value.
"""

from types import MappingProxyType
from typing import Any

from agent.collectors.registry import (
    Collector,
    FieldPolicy,
    mapping_projector,
    register,
    safe,
)
from agent.tools import infra

KEY = "host"

# load1 stays in the diff, banded, rather than joining uptime_s as excluded.
# It's the noisiest field here by a wide margin, but it is also the one
# signal that catches a runaway process pinning the host's CPU when nothing
# else has - guests, zfs_pool's state, and a monitor going down all report
# on symptoms downstream of that, sometimes minutes later, sometimes not at
# all (a busy but not-yet-failing process). Dropping it trades a real,
# distinct incident class for less banding work. With the full-step
# deadband it doesn't need dropping: fed the real oscillating sample that
# broke the half-step version (0.69/0.76/0.65/0.78/0.71, 20s apart,
# straddling the 0.5/1.0 edge), it now reports nothing across any
# consecutive pair, and a sustained climb to 3.0 still reports once - see
# tests/agent/test_tick.py.
POLICY = FieldPolicy(
    excluded=frozenset({"uptime_s"}),
    banded=MappingProxyType({"load1": 0.5, "mem_used_gb": 1.0, "arc_gb": 1.0}),
    exact=frozenset({"mem_total_gb"}),
)


def collect() -> dict[str, Any]:
    return {KEY: safe(infra.host_metrics)}


COLLECTOR = register(
    Collector(
        name="host",
        keys=(KEY,),
        collect=collect,
        projectors=MappingProxyType({KEY: mapping_projector(POLICY)}),
        policies=MappingProxyType({KEY: POLICY}),
    )
)
