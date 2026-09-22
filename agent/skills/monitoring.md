---
name: monitoring
description: Uptime Kuma, Beszel, Grafana, the anomaly detector and the host's other watchers - what each one watches, where the logs are, and how to read an alert.
---

# Monitoring

## Uptime Kuma, :3001 (stack `monitoring`)

Status page for every service. `monitors_status` reads it. A monitor down while the service answers when you curl it yourself usually means Kuma lost the network or the monitor URL is stale; check from LXC 101 with `curl -sI http://10.0.0.165:<port>`.

## Beszel, :8090 (stack `monitoring`)

CPU, RAM, disk and network history. Agents run on the host (beszel-agent.service), LXC 101, LXC 102 and VM 200. Use it to see trends; use `host_metrics` for the current number.

## Grafana, :3002 (stack `grafana`)

Dashboards, auto-detected by the runbook generator. Not wired to any alert you own.

## Host-side watchers (cron on 10.0.0.2)

| Script | When | Log |
|---|---|---|
| /opt/homelab/anomaly/anomaly.py sample | every 5 min | /tank/dev/anomaly/cron.log |
| /opt/homelab/anomaly/anomaly.py detect | every 15 min | /tank/dev/anomaly/cron.log |
| /opt/homelab/netwatch.py | every 10 min | /tank/dev/netwatch/netwatch.log |
| /opt/homelab/track-changes.sh | 03:45 daily | /var/log/homelab-changes.log |
| /opt/homelab/digest.sh | 07:00 daily | posts the morning digest |
| /opt/homelab/weekly-report.py | Sunday 18:00 | /var/log/homelab-weekly.log |

These belong to the legacy bot. See skill `legacy-host-bot` before touching them.

## Host under load

1. `host_metrics` for load, memory and ARC.
2. `host_exec` `top -b -n1 | head -25` to name the process. `qm`/`kvm` for VM 200, `lxc-start` children for containers.
3. Load spikes near 03:00 come from the vzdump backup and are normal. Load spikes on the second Sunday after midnight come from the scrub.
4. The CPU has 8 threads. Load under 8 is fine. Sustained load over 12 with guests slowing down is worth a report.

## Your own 60-second tick

The tick only wakes on diffs from its collectors (host, guests, ZFS, backups, certs, MT5 bands). A problem outside those is invisible to it. The improvement cycle exists to notice those gaps.
