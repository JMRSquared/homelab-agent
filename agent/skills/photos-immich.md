---
name: photos-immich
description: Immich, the family photo library - searching, downloading, stats, the containers behind it, and fixing uploads, search, or a down server. Also covers why the photos are not safe off-site.
---

# Photos: Immich

http://10.0.0.165:2283, stack `immich` in LXC 101. Users: Lavhe (the owner, also called Tech) and Kamo.

## Where the photos live

- Immich uploads: tank/immich, mounted at /mnt/immich in LXC 101 (~73G).
- Older archive on tank/media (the bulk of the 692G).
- Postgres runs on the NVMe inside LXC 101, backed up nightly with the guest.
- Nothing is off-site. See skill `backups`.

## Tools

- `photos_search` for "find the photo of X". Smart search matches descriptions, places and faces.
- `photos_download` to fetch one into /tank/dev/agent/outbox for `send_email` or Slack. It returns a JPEG by default because originals can be large TIFFs that mail servers reject. Keep the JPEG.
- `photos_stats` for counts and storage.
- `image_inspect` to look at the downloaded file and confirm it matches the request before sending it.

Search quality varies. Check what you got before sending it to someone. If the top hit is wrong, try other words (place, year, person's name) before giving up.

## Containers

immich_server (web and API), immich_machine_learning (CLIP search and face detection, CPU only, slow is normal), immich_postgres, immich_redis.

## Faults and fixes

- Web UI down: `guest_exec` 101 `docker ps -a | grep immich`, then `docker logs --tail 80 immich_server`. Postgres not ready is the usual cause; restart the `immich` stack.
- Search returns nothing: machine learning container down or still indexing. `docker logs --tail 50 immich_machine_learning`.
- Phone uploads stuck: the phone must reach 10.0.0.165, over Tailscale when away from home. Check `monitors_status` and that immich_server is up before blaming the phone.
- Disk full: see skill `storage-zfs`. Do not delete photos to make room.

## Rules

- Search and download are read-only. Never delete, archive, or edit assets or albums unless the person who owns them asks.
- Saving Immich settings in the UI overwrites direct database edits.
- Photos are private family data. Share them only with the person who asked, in the place they asked.
