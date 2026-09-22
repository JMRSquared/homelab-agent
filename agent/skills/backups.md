---
name: backups
description: Proxmox vzdump backups of every guest, retention, the missing off-site copy, and how to restore a guest. Read for any backup, restore, or "is X backed up" question.
---

# Backups

## What runs

Proxmox job `daily-guests` (/etc/pve/jobs.cfg):
- 03:00 every day, all guests, snapshot mode, zstd.
- Keeps 3 daily and 2 weekly.
- Writes to storage `backups`, which is /tank/backups on the same pool.
- Emails only on failure. The Gmail notification endpoint was never finished, so failures may go unannounced. Check it yourself.
- Excludes /opt/stacks/ai/ollama in LXC 102. Models re-download.

## The gap

No off-site copy exists. Backups sit on the same pool and in the same building as the originals. A fire, theft, or a two-disk loss in one vdev takes the 692G photo archive and every backup with it. `backup_coverage` reports which datasets have an off-site copy (none do) and which data is irreplaceable. When anyone asks "are my photos safe", give this answer plainly.

## Check a backup ran

- `host_exec`: `ls -lt /tank/backups/dump | head -20` for the newest archive per guest (vzdump-lxc-<id>-..., vzdump-qemu-200-...).
- `host_exec`: `grep -h "INFO: Finished Backup\|ERROR" /tank/backups/dump/*.log | tail -20`.
- A guest with no archive newer than ~26 hours means last night's run failed for it. Report which guest.

## Restore (only when the owner asks)

Restoring overwrites a guest. Confirm the guest id and archive with the owner first.
1. List archives: `host_exec` `ls /tank/backups/dump | grep -- -<id>-`.
2. Stop the guest (`guest_action` stop; 101 and 104 cannot be stopped by you, ask the owner).
3. LXC: `pct restore <id> /tank/backups/dump/<archive> --storage local-lvm --force`. VM: `qmrestore /tank/backups/dump/<archive> <id> --storage local-lvm --force`. Use `job_start`; restores take longer than five minutes.
4. Start it and verify the services inside respond.
5. After restoring LXC 102: `ollama pull qwen2.5:3b llama3.2:3b qwen2.5:7b` inside it.

## Before risky changes

Take `zfs_snapshot` of the affected dataset. For a guest, `host_exec` `vzdump <id> --storage backups --mode snapshot` via `job_start` gives a fresh backup first.
