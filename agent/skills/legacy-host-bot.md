---
name: legacy-host-bot
description: The older Slack bot and cron scripts in /opt/homelab on the Proxmox host - what each script posts, its schedule, its channel, and how to change one. Read when a Slack post did not come from you.
---

# Legacy host bot (/opt/homelab on 10.0.0.2)

A separate Slack app, older than you, runs on the host. You cannot talk to it through Slack. You reach its files with `host_exec`.

## Services

- `slackbot.service` runs /opt/homelab/slackbot.py in Socket Mode. It answers regex commands: search, watch, chart, digest, faceswap, help, media, panic, scan, silence, space, status, tazzie, unpanic, voice, voices. Command code lives in /opt/homelab/commands/.
- `faceswap-worker.service` runs /opt/homelab/faceswap-worker.py: Slack request, FaceFusion on CPU in the `faceswap` container in LXC 101, result back to Slack.

Restart with `host_exec` `systemctl restart slackbot` (or `faceswap-worker`), then `systemctl is-active` and `journalctl -u slackbot -n 30 --no-pager` to confirm.

## Cron jobs

| Script | Schedule | Output |
|---|---|---|
| tazzie-status.py | hourly on the hour | MT5 status card to #homelab-mt5, log /tank/dev/tazzie-metrics/status.log |
| tazzie-tradealert.py | every 3 min | trade alerts, /tank/dev/tazzie-alerts/cron.log |
| tazzie-export.py | every 15 min | /tank/dev/tazzie-metrics/export.log |
| tazzie-ocr-equity.py | every 10 min | equity read off the screen, /tank/dev/tazzie-metrics/ocr.log |
| tazzie-pnl.py | 00:05 daily | daily P&L, /tank/dev/tazzie-metrics/pnl.log |
| /root/tazzie-daily.sh | hourly at :05 | /tank/dev/tazzie-daily/YYYY-MM-DD.log |
| /root/tazzie-heal.sh | every 2 min | Algo Trading auto-heal, /tank/dev/tazzie-daily/heal.log |
| backtest-queue.py | every minute | /tank/dev/backtest-queue/watcher.log |
| photo-of-day.py | 08:00 daily | photo of the day, /var/log/homelab-photo.log |
| /root/income-report.sh | 08:00 daily | income report to #homelab-income |
| digest.sh | 07:00 daily | morning digest |
| weekly-report.py | Sunday 18:00 | /var/log/homelab-weekly.log |
| gen-runbook.sh | 03:30 daily | regenerates /tank/dev/HOMELAB.md |
| track-changes.sh | 03:45 daily | /var/log/homelab-changes.log |
| netwatch.py, anomaly.py | see skill `monitoring` | |

Find a job's line with `host_exec` `grep -r <script> /etc/cron.d/ ; crontab -l | grep <script>`.

## Channels

/etc/homelab/channels.conf maps each service to a channel: mt5=#homelab-mt5, alerts=#homelab-alerts, net=#homelab-net, media=#homelab-media, movie=#homelab-movies, videogen=#homelab-video-generation, digest and default=#homelab-notifications, income=#homelab-income. Scripts post through /opt/homelab/notify-slack.sh "<text>" <service>.

## Changing something

Nothing under /opt/homelab changes as a side effect of other work. When the owner asks for a change (post less often, move a report, fix a script):
1. Read the script and its cron line first.
2. Back it up: `cp file file.bak-$(date +%s)`.
3. Make the edit. For more than a line, write the whole file with a heredoc.
4. Run it once by hand and read the output, or wait for the next scheduled run and read its log.
5. Tell the owner what changed and where the backup is.
