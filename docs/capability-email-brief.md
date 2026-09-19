# Goal: the agent composes and sends a multi-source email, unaided

The target request, in the user's words:

> Send an email with an attachment from immich of a black audi rs3 to
> mulavhe@gmail.com, also link a screenshot from MT5 and the last movie we
> watched on Jellyfin and how long was it, using the mail container
> (admin@mail.jmrsquared.com)

The email must land in that mailbox with **no missing information**, composed and
sent by the agent itself rather than by a script someone wrote for this one task.

## Why this is the right exercise

It is four unrelated systems, three artifact types, one delivery channel, and a
model that has to sequence them without being told the order. Each piece is
independently verifiable, so a failure localises instead of turning into "it
didn't work".

## Reconnaissance (done, against the live homelab)

| Source | Finding |
|---|---|
| Immich | v3.1.0. Smart search `black audi rs3` returns 3 hits. `GET /api/assets/{id}/original` → 200 `image/tiff`; `/thumbnail?size=preview` → 200 `image/jpeg` |
| Jellyfin | v12.0.0. Watch history lives under user `root`, **not** `Kamo`. Last watched: *Mutiny* (2026), 95 min, 2026-09-16T17:35Z. Needs `/Users/{uid}/Items?SortBy=DatePlayed&Filters=IsPlayed` |
| MT5 | `qm monitor 200` → `screendump` produces a 1280×800 PPM (~3 MB). `ffmpeg` is on the host for conversion |
| Mail | Stalwart on LXC 103. Listening: 25, 465 (implicit TLS), 993, 995, 8080, 443, 8443, 4190. Sending to gmail.com is a relay, so it needs authentication as `admin@mail.jmrsquared.com` |

## Capability gaps

The agent has 18 tools and none of them can do any of the four. It is not a
prompting problem; there is no route.

1. Fetch an Immich asset **to a file** — `photos_search` returns metadata only
2. Ask Jellyfin what was watched last and for how long
3. Capture an MT5 screenshot as an attachable image
4. Send email with attachments

## Goals

Each goal is independently testable and lands on its own.

- **G1 — artifact workspace.** A known directory the agent writes to
  (`/tank/dev/agent/outbox/`), on the existing bind mount, with cleanup so it
  cannot grow without bound.
- **G2 — `photos_download(query)`.** Smart-search Immich, download the best
  match, return the local path, dimensions and size. Prefer a JPEG rendition
  over a 60 MB TIFF unless asked otherwise: an attachment has to be mailable.
- **G3 — `media_last_watched()`.** Last played item across *all* Jellyfin users,
  with title, year, runtime in minutes and when it was watched. Must not assume
  a single user — the history is under `root`, which is not the obvious one.
- **G4 — `mt5_screenshot()`.** Screendump VM 200 via `qm monitor`, convert PPM to
  PNG, return the path. Must not disturb the terminal.
- **G5 — `send_email(to, subject, body, attachments)`.** SMTP over 465 to LXC
  103, authenticated. Multipart, real attachments, returns the message id the
  server assigned.
- **G6 — teach.** Write what the agent needs to know into its brain and prompt:
  which Jellyfin user holds history, that TIFFs are too big to mail, the
  ticks→minutes conversion, the outbox path.
- **G7 — drill.** Ask it to do each piece unaided. When it fails, diagnose
  whether the gap is a missing tool, an unclear description, or absent
  knowledge. Fix that specific thing. Re-ask. Only move on when it succeeds
  without hints.
- **G8 — the whole thing.** Give it the user's sentence verbatim and confirm the
  email arrives complete.

## Blocker

`send_email` needs the password for `admin@mail.jmrsquared.com`. Only the owner
has it; it goes into `/etc/homelab-agent/env` via `deploy/set-secrets.sh`, which
never echoes it.

## Definition of done

The agent receives the user's sentence, and an email arrives at
mulavhe@gmail.com containing: the Audi photo attached and openable, an MT5
screenshot, the correct film title and runtime, sent from
admin@mail.jmrsquared.com. No placeholders, nothing silently dropped.
