"""Guards agent/main.py's tool-import line.

Tool registration happens by import side effect: `agent/tools/base.py`'s
`@tool` decorator populates `REGISTRY` when its module is imported.
`agent/main.py` is the one place that imports every tool module, so a
module missing from that line means its tools silently never register -
nothing raises, the agent just ships without that capability.

This test forces a fresh import of `agent.main` (evicting both it and
every tool module from `sys.modules`, and clearing `REGISTRY`, first) so
the only thing that can populate `REGISTRY` here is `agent/main.py`'s own
import line - not some other test module that happened to import
`agent.tools.household` earlier in the run. It never calls
`amain`/`main`, only triggers `agent/main.py`'s top-level imports.

Popping `sys.modules` alone is not enough: CPython's `from package import
name` only re-imports `name` when `package` lacks that attribute
(`importlib._bootstrap._handle_fromlist`). Every other test module in this
suite imports these same tool modules directly (e.g.
`from agent.tools import household`), which leaves `agent.tools.household`
bound as an attribute on the `agent.tools` package - so popping only
`sys.modules["agent.tools.household"]` would make `from agent.tools import
household` in `agent/main.py` silently reuse that stale attribute instead
of re-executing the module and re-registering its tools. This test also
strips the attribute off the package object, so the import is genuine.
"""

import sys

import pytest

import agent.tools as tools_pkg
from agent.tools import base

TOOL_MODULES = [
    "agent.tools.comms",
    "agent.tools.household",
    "agent.tools.infra",
    "agent.tools.mail",
    "agent.tools.media",
    "agent.tools.memory",
    "agent.tools.mt5_screenshot",
    "agent.tools.photos",
    "agent.tools.vision",
]


@pytest.fixture(autouse=True)
def _clean_registry():
    snapshot = dict(base.REGISTRY)
    yield
    base.REGISTRY.clear()
    base.REGISTRY.update(snapshot)


def test_main_imports_every_tool_module_and_registers_its_tools():
    base.REGISTRY.clear()
    for name in TOOL_MODULES:
        sys.modules.pop(name, None)
        short = name.rsplit(".", 1)[-1]
        if hasattr(tools_pkg, short):
            delattr(tools_pkg, short)
    sys.modules.pop("agent.main", None)

    import agent.main  # noqa: F401

    # Every tool the modules agent/main.py must import register, not just
    # one representative per module - a module could import cleanly while
    # one of its own tools silently failed to register (a decorator
    # ordering bug, a duplicate name overwriting another), and a
    # one-per-module check would miss that.
    expected = {
        # comms
        "slack_say",
        "slack_history",
        "slack_thread_replies",
        # household
        "notes_append",
        # infra
        "guests_list",
        "guest_action",
        "guest_exec",
        "host_exec",
        "mt5_status",
        "zfs_report",
        "zfs_snapshot",
        "host_metrics",
        "docker_stacks",
        "docker_action",
        "monitors_status",
        "adguard_report",
        # mail
        "send_email",
        "mail_list_messages",
        "mail_read_message",
        # media
        "media_search",
        "media_request",
        "media_last_watched",
        "media_library_status",
        # memory
        "brain_read",
        "brain_write",
        # mt5_screenshot
        "mt5_screenshot",
        # photos
        "photos_search",
        "photos_download",
        "photos_stats",
        # vision
        "image_inspect",
    }
    assert len(expected) == 30
    assert base.REGISTRY.keys() == expected, (
        f"missing: {expected - base.REGISTRY.keys()}, "
        f"unexpected: {base.REGISTRY.keys() - expected}"
    )
