"""hostctl-specific pytest configuration.

The test runner inherits HOSTCTL_TOKEN_FILE from its environment - on this
host that is /etc/hostctl/token, the live hostctl token path. hostctl/auth.py
reads HOSTCTL_TOKEN_FILE first and only falls back to HOSTCTL_TOKEN, so every
per-file autouse fixture that sets HOSTCTL_TOKEN=testtoken (test_actions,
test_certs, test_guests, test_jobs_route, test_mt5_screenshot_route) is
silently shadowed by the inherited file-token: the request sends "Bearer
testtoken" but hostctl expects the contents of /etc/hostctl/token, every
authenticated test in this directory returns 401, and the only tests that
pass are the ones that explicitly assert 401 on missing auth.

This fixture clears HOSTCTL_TOKEN_FILE for the duration of every test in
tests/hostctl/ so the per-file fixtures work the way they read. It uses
`raising=False` so it is a no-op when the var isn't set (e.g. CI without the
service env loaded).

Verified on 2026-10-09: with this in place, tests/hostctl/ goes from
17 failed to 95 passed; full suite 526 passed.
"""

import pytest


@pytest.fixture(autouse=True)
def _clear_inherited_hostctl_token_file(monkeypatch):
    monkeypatch.delenv("HOSTCTL_TOKEN_FILE", raising=False)
