"""Collector registry plus every collector the tick sweeps.

Importing this package registers all of them (import side effect, the same
way `agent/tools` registers tools), so `agent/tick.py` importing this
module is the whole wiring. Import order below is the order `collect()`
merges its keys in.
"""

# Registration by import side effect: importing this package must import
# every collector module, so the registry is populated before the tick
# reads it. The submodule import is deliberately separate from (and after)
# the registry re-exports below, which is what the isort skip is for.
from agent.collectors import backup, certs, guests, host, mt5, zfs  # noqa: F401  isort:skip
from agent.collectors.registry import (
    Collector,
    FieldPolicy,
    Projector,
    collect_all,
    mapping_projector,
    nested_mapping_projector,
    policies,
    project_all,
    projectors,
    register,
    registered,
    safe,
)

__all__ = [
    "Collector",
    "FieldPolicy",
    "Projector",
    "backup",
    "certs",
    "collect_all",
    "guests",
    "host",
    "mapping_projector",
    "mt5",
    "nested_mapping_projector",
    "policies",
    "project_all",
    "projectors",
    "register",
    "registered",
    "safe",
    "zfs",
]
