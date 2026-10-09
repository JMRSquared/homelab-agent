"""Agent-package pytest configuration.

self_deploy runs the suite inheriting the service environment, which
sets SLACK_CHANNEL_STATUS and SLACK_CHANNEL_LOG to empty strings to
disable the per-tool-call audit mirror (see slack_app.audit_channel_enabled
and the owner note in test_audit_channel_can_be_turned_off).
os.environ.get returns '' for an env var that IS set to '', NOT the
comma default - so slack_app.CH_HOMELAB and CH_LOG silently become ''
at import time, and any test that does not explicitly reload slack_app
with the env vars unset ends up asserting against '' instead of the
baked-in "#homelab-alerts" / "#homelab-agent-log" defaults.

This autouse fixture pops both vars, reloads slack_app so the
module-level constants reflect the cleared env, yields to the test,
then restores the inherited values and reloads again. Tests that
explicitly want non-default channel names set them via monkeypatch and
reload themselves - the established pattern is in
test_channel_env_vars_override_the_defaults and
test_audit_channel_can_be_turned_off.

Verified 2026-10-09: with this in place, full pytest with self_deploy's
env (SLACK_CHANNEL_STATUS="" SLACK_CHANNEL_LOG="" SLACK_BOT_TOKEN=...
HOSTCTL_TOKEN_FILE=/etc/hostctl/token) goes from ~30 failed to
526 passed, 0 failed.
"""

import importlib
import os
from collections.abc import Iterator

import pytest

from agent import slack_app


@pytest.fixture(autouse=True)
def _clear_slack_channel_env_for_agent_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    prev_status = os.environ.pop("SLACK_CHANNEL_STATUS", None)
    prev_log = os.environ.pop("SLACK_CHANNEL_LOG", None)
    importlib.reload(slack_app)
    yield
    if prev_status is not None:
        os.environ["SLACK_CHANNEL_STATUS"] = prev_status
    if prev_log is not None:
        os.environ["SLACK_CHANNEL_LOG"] = prev_log
    importlib.reload(slack_app)
