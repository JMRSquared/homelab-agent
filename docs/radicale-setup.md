# Radicale setup checklist

One-time manual deployment and account setup for the family calendar. Nothing here
runs automatically — the owner runs these commands by hand and confirms each one, the
same as every other deployment step in this project. **None of this has been run yet.**

Radicale is a small CalDAV server. It stores the calendar as plain `.ics` files on
disk and speaks the CalDAV protocol that iPhone, Android, and desktop calendar apps
already understand natively — no app to install on any family phone, no Home
Assistant. The agent talks to the same server over the same protocol via
`agent/tools/household.py`.

## 1. Deploy the stack and create the family account

The htpasswd file that gates access has to exist before the calendar is genuinely
usable, but `htpasswd -c` (used to create it) runs via `docker exec` — which needs
the container already running. So this happens in three ordered sub-steps, run
back to back: bring the stack up, immediately create the account, then restart so
Radicale picks up the new auth file. Do not leave a gap between them.

Run from a machine with SSH access to the Proxmox host, after reviewing
`deploy/stacks/radicale/compose.yaml`:

```bash
# 1a. Push the compose file and bring the container up.
ssh -n -o BatchMode=yes root@10.0.0.2 'pct exec 101 -- mkdir -p /opt/stacks/radicale'
scp deploy/stacks/radicale/compose.yaml root@10.0.0.2:/tmp/radicale-compose.yaml
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct push 101 /tmp/radicale-compose.yaml /opt/stacks/radicale/compose.yaml'
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 101 -- docker compose -f /opt/stacks/radicale/compose.yaml up -d'

# 1b. Immediately create the family account — the container has to be up for
# `docker exec` to reach it, so this is the earliest point it can happen.
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 101 -- docker exec -it radicale htpasswd -B -c /data/htpasswd family'

# 1c. Restart so Radicale picks up the htpasswd file it didn't have at first boot.
ssh -n -o BatchMode=yes root@10.0.0.2 \
  'pct exec 101 -- docker compose -f /opt/stacks/radicale/compose.yaml restart'
```

Step 1b prompts for a password interactively — set one and store it as
`CALDAV_PASSWORD` for the agent (see step 3). Add further family accounts the same
way, dropping `-c` so the file isn't recreated (no restart needed for an addition,
only for the first account):

```bash
pct exec 101 -- docker exec -it radicale htpasswd -B /data/htpasswd <name>
```

Radicale creates a calendar collection for a user the first time that user's client
connects, so the first phone to subscribe as `family` creates the shared calendar at
`http://10.0.0.165:5232/family/home/`. All family members subscribe to that same
`family` account and URL rather than each having a separate calendar — the whole point
is one shared calendar everyone sees.

Check the stack started with `docker_stacks` (already-registered agent tool) or
`pct exec 101 -- docker ps`.

## 2. Verify the agent's CalDAV URL actually resolves

`CALDAV_URL` below (`http://10.0.0.165:5232/family/home/`) is a guess at a calendar
*collection* path, carried over unverified from the original plan — nobody has
confirmed `caldav`'s `principal()` discovery actually works from it, because this
deployment has never been run. Before wiring the agent up, check this by hand, once
Radicale is running and the `family` account exists:

```bash
python3 -c "
from caldav.davclient import DAVClient
client = DAVClient(
    url='http://10.0.0.165:5232/family/home/',
    username='family',
    password='<the password from step 1b>',
)
principal = client.principal()
for cal in principal.calendars():
    print(cal.url)
"
```

If this prints the family calendar's URL, `CALDAV_URL` as given is correct — use it
as-is in step 3. If it errors (a 404, or "not a principal"), the value is wrong: the
likely fix is pointing `CALDAV_URL` at the *principal root* instead of the collection
path — try `http://10.0.0.165:5232/family/` (drop the trailing `home/`) and repeat
the check. Don't guess further than that without re-running this check; use whichever
URL actually makes `principal()` resolve.

## 3. Point the agent at it

Set these three environment variables wherever the agent process runs (never commit
them):

| Variable | Value |
|---|---|
| `CALDAV_URL` | `http://10.0.0.165:5232/family/home/` |
| `CALDAV_USER` | `family` |
| `CALDAV_PASSWORD` | the password set in step 2 |

Also set `AGENT_LISTS` if `/tank/dev/agent/lists` (the default) isn't where the shared
shopping/chores lists should live.

## 4. Subscribe a family phone (non-technical, step by step)

### iPhone / iPad

1. Open **Settings** → **Calendar** → **Accounts** → **Add Account**.
2. Tap **Other**, then **Add CalDAV Account**.
3. Fill in:
   - **Server**: `10.0.0.165`
   - **User Name**: `family`
   - **Password**: the password from step 2
   - **Description**: `Family Calendar`
4. Tap **Next**. If it warns the connection isn't encrypted, tap **Continue** anyway —
   this server only runs on the home network, it never leaves the house Wi-Fi.
5. Make sure **Calendars** is turned on, then tap **Save**.
6. Open the **Calendar** app — the family calendar now shows up alongside any other
   calendars, and anything added on any phone appears on all of them within a minute
   or two.

### Android

Android's own Calendar app doesn't speak CalDAV directly, so use a small free app —
**DAVx⁵** is the standard one (Play Store).

1. Install **DAVx⁵**.
2. Open it and tap **+** → **Login with URL and username**.
3. Fill in:
   - **Base URL**: `http://10.0.0.165:5232/family/home/`
   - **User name**: `family`
   - **Password**: the password from step 2
4. Tap **Login**, then confirm adding the calendar it finds.
5. Open the regular **Calendar** app (Google Calendar or whichever is default) and
   make sure the new "Family Calendar" account is checked so it's visible there too.

### Desktop / other

Any calendar app that supports CalDAV (Outlook, Thunderbird with an add-on, macOS
Calendar) takes the same three values: server `10.0.0.165:5232`, path
`/family/home/`, user `family`, and the password from step 2.

## Notes

- Radicale only listens on the home network (`10.0.0.165`), so nothing here is
  reachable from outside the house — there's no need for HTTPS on this setup, though
  it can be added later behind a reverse proxy if the household ever wants remote
  calendar access.
- If a subscribed phone stops showing new events, the fastest check is
  `calendar_list` from the agent (in Slack) to confirm the server itself has them,
  which narrows the problem to the phone's own sync interval.
