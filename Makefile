HOST := root@10.0.0.2
CT   := 104

test:
	pytest -q && ruff check . && mypy --strict agent hostctl

# LXC 104 has its own clone of this repo (see docs/deploy.md) and can only
# ever fetch what GitHub already has, so deploying is: make sure this branch
# is exactly what's on GitHub, then tell the container to pull, reinstall
# (pyproject.toml is the single source of truth for runtime dependencies, so
# this is what makes a dependency change actually take effect), and restart.
#
# Refuses a dirty working tree or a HEAD that doesn't match what was just
# pushed - either one means the container would silently redeploy the
# previous commit while this command reports success.
deploy: test
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "deploy refused: working tree has uncommitted or untracked changes" >&2; \
		exit 1; \
	fi
	git push origin HEAD
	@if [ "$$(git rev-parse HEAD)" != "$$(git rev-parse '@{u}' 2>/dev/null)" ]; then \
		echo "deploy refused: local HEAD does not match the pushed upstream ref" >&2; \
		exit 1; \
	fi
	ssh -n $(HOST) 'pct exec $(CT) -- git -C /opt/homelab-agent pull'
	ssh -n $(HOST) 'pct exec $(CT) -- /opt/homelab-agent/.venv/bin/pip install /opt/homelab-agent'
	ssh -n $(HOST) 'pct exec $(CT) -- systemctl restart homelab-agent'
	ssh -n $(HOST) 'pct exec $(CT) -- systemctl is-active homelab-agent'
	ssh -n $(HOST) 'pct exec $(CT) -- git -C /opt/homelab-agent rev-parse --short HEAD'
