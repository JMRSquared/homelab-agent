"""Off-site backup coverage as tick state.

Growth in unprotected irreplaceable data is a real signal - another 40G of
family photos with nowhere off-site to land is worth saying out loud. A
photo import writing a few hundred megabytes is not, and neither is the
byte-level churn every dataset shows all day. So every size here is banded
at `SIZE_BAND_GB` with a full-step deadband, and the timestamps the report
carries (`generated_at`, `protection_changed_at`) are excluded outright -
`generated_at` changes on literally every tick by construction.

What is compared exactly is the thing that matters: which datasets are
classified how, and whether each one has an off-site copy. A dataset going
from protected to unprotected wakes the model immediately, at any size.
"""

from types import MappingProxyType
from typing import Any

from agent import backup_coverage
from agent.collectors import zfs as zfs_collector
from agent.collectors.registry import (
    Collector,
    FieldPolicy,
    Projector,
    mapping_projector,
    nested_mapping_projector,
    register,
)

KEY = "backup"

# 10 GB. On the live pool that is ~13% of tank/immich and ~1.4% of
# tank/media: coarse enough that ordinary ingest noise never registers,
# fine enough that a genuine import shows up within a day.
SIZE_BAND_GB = 10.0

DATASET_POLICY = FieldPolicy(
    banded=MappingProxyType({"size_gb": SIZE_BAND_GB}),
    exact=frozenset(
        {"classification", "note", "protection", "offsite_target", "snapshot_count",
         "snapshots_only"}
    ),
)

TOTALS_POLICY = FieldPolicy(
    banded=MappingProxyType(
        {
            "irreplaceable_gb": SIZE_BAND_GB,
            "irreplaceable_unprotected_gb": SIZE_BAND_GB,
            "protected_gb": SIZE_BAND_GB,
            "unclassified_gb": SIZE_BAND_GB,
        }
    )
)

TOP_POLICY = FieldPolicy(
    excluded=frozenset({"generated_at", "protection_changed_at", "summary"}),
    exact=frozenset(
        {"datasets", "totals", "unprotected_irreplaceable", "unclassified",
         "snapshots_are_not_backups"}
    ),
)

_datasets_projector = nested_mapping_projector(DATASET_POLICY)
_totals_projector = mapping_projector(TOTALS_POLICY)


def collect() -> dict[str, Any]:
    zfs = zfs_collector.fetch()
    if "error" in zfs:
        return {KEY: zfs}
    datasets = zfs.get("datasets")
    if not isinstance(datasets, list):
        return {KEY: {"error": "hostctl /zfs/status returned no dataset listing"}}
    return {KEY: backup_coverage.report(datasets)}


def project(value: Any, previous: Any) -> Any:
    if not isinstance(value, dict) or "error" in value:
        return value
    prev = previous if isinstance(previous, dict) else {}
    projected: dict[str, Any] = {}
    for key, item in value.items():
        if key in TOP_POLICY.excluded:
            continue
        if key == "datasets":
            projected[key] = _datasets_projector(item, prev.get(key))
        elif key == "totals":
            projected[key] = _totals_projector(item, prev.get(key))
        else:
            projected[key] = item
    return projected


_PROJECTORS: dict[str, Projector] = {KEY: project}

COLLECTOR = register(
    Collector(
        name="backup",
        keys=(KEY,),
        collect=collect,
        projectors=MappingProxyType(_PROJECTORS),
        policies=MappingProxyType({KEY: TOP_POLICY}),
    )
)
