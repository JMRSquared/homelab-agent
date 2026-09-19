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
    "agent.tools.media",
    "agent.tools.memory",
    "agent.tools.photos",
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

    # One representative tool per module: comms, household, infra, media,
    # memory, photos - the six modules agent/main.py must import.
    expected = {
        "slack_say",  # comms
        "calendar_add",  # household
        "guest_action",  # infra
        "media_search",  # media
        "brain_write",  # memory
        "photos_search",  # photos
    }
    missing = expected - base.REGISTRY.keys()
    assert not missing, f"tools missing from REGISTRY after importing agent.main: {missing}"
