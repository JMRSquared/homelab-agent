HOST := root@10.0.0.2
CT   := 103

test:
	pytest -q && ruff check . && mypy --strict agent hostctl

deploy: test
	tar czf /tmp/agent.tgz agent pyproject.toml
	scp /tmp/agent.tgz $(HOST):/tmp/
	ssh -n $(HOST) 'pct push $(CT) /tmp/agent.tgz /tmp/agent.tgz'
	ssh -n $(HOST) 'pct exec $(CT) -- tar xzf /tmp/agent.tgz -C /opt/homelab-agent'
	ssh -n $(HOST) 'pct exec $(CT) -- systemctl restart homelab-agent'
	ssh -n $(HOST) 'pct exec $(CT) -- systemctl is-active homelab-agent'
