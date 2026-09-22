"""Which datasets are irreplaceable, and where an off-site copy of each
one is meant to live.

This is configuration, not logic. Whether `tank/media` matters is a
judgement about the owner's life - it is 692G of Real-Debrid-sourced
films that a re-fetch reproduces, so losing it costs time, not memories -
while `tank/immich` is 73G of family photos that nothing reproduces. That
judgement belongs in data the owner can edit, not in a branch somewhere
in the coverage code.

Loaded the same way `agent/config.py` loads everything else: a default
built into the module, overridable by an env var pointing at a JSON file.
`AGENT_BACKUP_POLICY=/tank/dev/agent/backup-policy.json` and the file wins.

`offsite` is the *declared* off-site destination per dataset. It is empty
today because there genuinely is no off-site copy of anything: `tank/backups`
holds Proxmox VM dumps and templates, on the same pool, which protects
against a bad upgrade and against nothing else.
"""

import json
import os
from typing import Any

IRREPLACEABLE = "irreplaceable"
REPRODUCIBLE = "reproducible"
UNKNOWN = "unknown"
CLASSIFICATIONS = (IRREPLACEABLE, REPRODUCIBLE, UNKNOWN)

DEFAULT_POLICY: dict[str, Any] = {
    "datasets": {
        "tank/immich": {
            "classification": IRREPLACEABLE,
            "note": "family photos and videos - nothing anywhere reproduces these",
        },
        "tank/media": {
            "classification": REPRODUCIBLE,
            "note": "films and shows sourced through Real-Debrid; re-fetchable, costs time only",
        },
        "tank/backups": {
            "classification": REPRODUCIBLE,
            "note": (
                "Proxmox VM dumps and templates - reproducible from the running guests, "
                "and on the same pool, so not a backup of anything else here"
            ),
        },
    },
    # Anything the owner hasn't classified yet. Deliberately not
    # "reproducible": an unreviewed dataset quietly defaulting to "safe to
    # lose" is how data goes missing.
    "default_classification": UNKNOWN,
    # dataset name -> {"target": str, "kind": str, "since": ISO8601 str}
    "offsite": {},
}


def load_policy() -> dict[str, Any]:
    """The backup policy, from `AGENT_BACKUP_POLICY` if it points at a
    readable JSON file, else the module default.

    A broken or missing override falls back to the default rather than
    raising: a typo'd path must not take the tick down, and reporting
    coverage against the built-in classification is strictly better than
    reporting nothing.
    """
    path = os.environ.get("AGENT_BACKUP_POLICY")
    if not path:
        return DEFAULT_POLICY
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_POLICY
    if not isinstance(loaded, dict):
        return DEFAULT_POLICY
    policy = dict(DEFAULT_POLICY)
    policy.update(loaded)
    return policy


def classification_of(policy: dict[str, Any], dataset: str) -> tuple[str, str]:
    """(classification, note) for one dataset name."""
    entries = policy.get("datasets")
    entry = entries.get(dataset) if isinstance(entries, dict) else None
    default = str(policy.get("default_classification", UNKNOWN))
    if not isinstance(entry, dict):
        return default, "not classified in the backup policy"
    classification = entry.get("classification")
    if classification not in CLASSIFICATIONS:
        classification = default
    return str(classification), str(entry.get("note", ""))


def offsite_target(policy: dict[str, Any], dataset: str) -> dict[str, Any] | None:
    """The declared off-site destination for a dataset, or None."""
    offsite = policy.get("offsite")
    if not isinstance(offsite, dict):
        return None
    entry = offsite.get(dataset)
    return entry if isinstance(entry, dict) else None
