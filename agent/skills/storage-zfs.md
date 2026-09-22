---
name: storage-zfs
description: The ZFS pool `tank` - disks, vdevs, datasets, capacity, scrubs, snapshots, and what to do when the pool degrades or fills up.
---

# Storage: ZFS pool `tank`

## Layout

Pool `tank`, 2.27T raw. Two vdevs, each survives one dead disk:
- `raidz1-0`: Seagate ST1000DM010 (serial Z9ANT54R) + WD WD10JPVX (WX31A54H1088)
- `mirror-1`: HGST HTS725050A7E630 (RC055ACB38EEUJ) + WD WD5000AZLX (WCC6Z1XPHELL)

Losing two disks in the same vdev loses the whole pool, photos included. Treat one faulted disk as urgent.

Guest boot disks do not live on tank. They sit on `local-lvm`, an LVM-thin volume on the 512GB NVMe.

## Datasets (2026-09-23)

| Dataset | Used | What |
|---|---|---|
| tank/media | 692G | Jellyfin local library and the photo archive. Mounted in LXC 101 at /mnt/media |
| tank/backups | 258G | Proxmox vzdump target (storage id `backups`) |
| tank/immich | 73G | Immich uploads. Mounted in LXC 101 at /mnt/immich |
| tank/dev | 3.9G | scratch, your brain and outbox (/tank/dev/agent), TazzieBot metrics, HOMELAB.md |
| tank/mail | small | Stalwart mail data for LXC 103 |
| tank/storj | small | Storj node identity, quota 200G. No node runs yet |

Pool sits at about 76% allocated with ~320G free per dataset. ZFS slows down past 80% and gets bad past 90%. Above 85%, tell the owner and name the biggest consumer (`zfs list -o name,used,usedbysnapshots -s used -r tank`). Snapshots on tank/backups often hold tens of GB.

## Routine

- Scrub: monthly, second Sunday 00:24 (/etc/cron.d/zfsutils-linux). Trim: first Sunday.
- ARC capped at 4GB in /etc/modprobe.d/zfs.conf. Leave it; the host needs the RAM for guests.
- SMART: smartd runs on the host. `smartctl -a /dev/disk/by-id/<id>` for one disk.

## Checks

1. `zfs_report` for health and usage.
2. `host_exec`: `zpool status -v tank` for per-disk errors and resilver progress.
3. `host_exec`: `zpool events -v | tail -50` for recent faults.

## When something is wrong

- DEGRADED with one disk FAULTED or UNAVAIL: the pool still serves data. Do not run `zpool clear`, `replace`, `offline` or `detach` on your own. Post to #homelab-alerts: which vdev, which disk (model and serial from the list above), and that a replacement disk is needed. The owner swaps hardware.
- Read/write/checksum errors climbing but disk ONLINE: pull `smartctl -a` for that disk, report reallocated and pending sector counts. One-off checksum errors after a power cut are often harmless; a rising count is a dying disk.
- Pool nearly full: find the consumer, report it. Do not delete data or snapshots to make room unless the owner says which.

## Hard rules

- Address disks by `/dev/disk/by-id/...`, never `/dev/sdX`. The sdX letters move between boots.
- Take `zfs_snapshot` before any change that rewrites a dataset's contents.
- Never run `zfs destroy`, `zpool destroy`, `zpool labelclear`, or `wipefs` on anything. That loses family photos that exist nowhere else.
