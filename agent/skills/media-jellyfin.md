---
name: media-jellyfin
description: Jellyfin, Jellyseerr, Zurg, rclone and Real-Debrid - how films and shows reach the TV, and how to fix "can't play", "missing film", or an empty library.
---

# Media: Jellyfin and Real-Debrid

## How it fits together

1. Someone adds a film or show on debridmediamanager.com (or through Jellyseerr at :5055, or your `media_request` tool).
2. Real-Debrid caches it in the cloud.
3. Zurg (:9999, container `zurg`) exposes the Real-Debrid library over WebDAV.
4. rclone (container `rclone`) mounts that at /mnt/zurg inside LXC 101, read-only.
5. Jellyfin (:8096) libraries /media/movies and /media/shows point into that mount.
6. Zurg's `on_library_update` hook (/opt/stacks/debrid/zurg/data/update.sh) calls Jellyfin's refresh, so new titles show up within about a minute.

Local media on tank/media (/mnt/media) is a separate Jellyfin library.

## Tools

- "Do we have X?": `media_search` first.
- "Get X": `media_search`, then `media_request` only when it is missing.
- "What did we watch?": `media_last_watched`. History sits under Jellyfin user `root`, not `Kamo`.
- Totals and pending requests: `media_library_status`.

Users: `root` (the owner's household account) and `Kamo`.

## Faults and fixes

- `media_request` or `media_library_status` returns HTTP 403 from http://10.0.0.165:5055/api/v1/request (or `/api/v1/search`): Jellyseerr is rejecting the API key the agent sends. `JELLYSEERR_KEY` IS set in `/etc/homelab-agent/env` (a base64-encoded UUID; decoded to `1789315383261fb32f26c-b9b0-42bd-b749-b553e1fa5098`), so the first-run wizard was run and an admin user + API key exist. `agent/media.py` sends the raw base64 value as `X-Api-Key` without decoding, which Jellyseerr rejects with 403. Three hypotheses for the 403: (a) Jellyseerr expects the base64-DECODED key, not the raw form -- a one-line decode in `agent/media.py` would test it (untested); (b) the key was rotated/revoked on Jellyseerr side since it was added to env -- owner fix only: log in to http://10.0.0.165:5055, Settings -> API Keys, regenerate or confirm the existing one is current; (c) Jellyseerr has an IP allowlist that excludes the agent's source IP -- owner fix only. While unresolved: `media_request` cannot be relied on; for "watch X" asks, point the requester at debridmediamanager.com (the documented fallback that always works). Do not pretend Jellyseerr works; do not waste rounds re-probing its API from the improvement cycle.
- Library empty or every title fails to play: the rclone mount died. `guest_exec` 101: `ls /mnt/zurg | head`. "Transport endpoint is not connected" or empty output means restart the `debrid` stack with `docker_action`, wait a minute, then check `ls /mnt/zurg` again.
- One title fails, others play: Real-Debrid dropped or never cached that torrent. Check `docker logs --tail 100 zurg` for its name. The fix is re-adding it on debridmediamanager.com; tell the person.
- New title not appearing: check Zurg logs for the update hook firing, then trigger a scan yourself: `curl -s -X POST -H 'Authorization: MediaBrowser Token="<JELLYFIN_KEY>"' http://10.0.0.165:8096/Library/Refresh`.
- Buffering on 4K: the host has no GPU for HEVC transcoding. Suggest a lower-quality version or a player that direct-plays.

## Rules

- Jellyfin 12's API needs the header `Authorization: MediaBrowser Token="KEY"`. `?api_key=` fails.
- Saving settings in the Jellyfin web UI overwrites edits made directly to its database. Change settings through the API or the UI, not SQLite.
- The Real-Debrid token lives in /opt/stacks/debrid/zurg/config.yml. Never print or post it.
- Jellyseerr's first-run wizard was completed (JELLYSEERR_KEY is in env and base64-decodes to a valid UUID), but `media_request` still 403s. Until the key/form mismatch is resolved, treat Jellyseerr's API as non-functional and direct "watch X" asks to debridmediamanager.com. See the Jellyseerr 403 entry under Faults and fixes for the three remaining hypotheses.
