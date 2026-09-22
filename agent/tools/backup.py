"""The `backup_coverage` tool: answer "is the family photo library backed
up?" directly, in conversation, without waiting for a tick to notice."""

from typing import Any

from agent import backup_coverage
from agent.tools import infra
from agent.tools.base import tool


@tool(
    "backup_coverage",
    "Report which ZFS datasets have an off-site copy and which do not, with sizes and "
    "how each one is classified (irreplaceable vs reproducible). Use this for any "
    "question about backups, off-site copies, or whether the family photos are safe. "
    "Read the result carefully before answering: local ZFS snapshots are NOT counted "
    "as backup coverage here and must never be reported as if they were - they live on "
    "the same pool as the data and are lost with it. A dataset showing "
    "snapshots_only=true is unprotected, however many snapshots it has. The "
    "classification comes from the owner's backup policy (agent/backup_policy.py, "
    "overridable via AGENT_BACKUP_POLICY); datasets listed under 'unclassified' have "
    "never been judged either way and are worth asking the owner about.",
    infra.NO_ARGS,
)
def backup_coverage_report() -> dict[str, Any]:
    report = infra.zfs_report()
    datasets = report.get("datasets")
    if not isinstance(datasets, list):
        raise RuntimeError("hostctl /zfs/status returned no dataset listing")
    return backup_coverage.report(datasets)
