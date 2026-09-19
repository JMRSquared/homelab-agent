# Slack setup checklist

One-time manual setup at `https://api.slack.com/apps`, needed before the agent process
can run. Nothing here touches the repo or secrets — do this once per workspace and hand
the resulting tokens to the deployment step.

Derived from what `agent/slack_app.py` and `agent/tools/comms.py` actually call, not
copied from the plan.

## 1. Create the app and enable Socket Mode

- Create a new app "from scratch" in the target workspace.
- Under **Socket Mode**, turn it on. This is what lets the agent run behind the
  Vodafone router with no inbound port forward — it dials out over a websocket
  instead of accepting webhooks.
- Generate an app-level token with the `connections:write` scope. This is the
  `xapp-` token (`SLACK_APP_TOKEN`).

## 2. Bot token scopes (OAuth & Permissions)

| Scope | Why |
|---|---|
| `app_mentions:read` | receive `app_mention` events when someone @-mentions the bot |
| `channels:history` | read messages in public channels the bot is in (`message.channels`) |
| `channels:read` | resolve `#homelab` / `#family` / `#agent-log` channel info |
| `chat:write` | `slack_say` posts via `chat.postMessage`, and `say()` replies in `handle_message` |
| `im:history` | read DM history (`message.im`) |
| `im:read` | list/open DM conversations |
| `im:write` | open a DM conversation with a user if needed |
| `users:read` | resolve user IDs to names for logging/replies |

`files:write` from the original plan is dropped: nothing in `agent/tools/comms.py` or
`agent/slack_app.py` uploads a file. Add it back only if a future tool needs
`files.upload`.

## 3. Event subscriptions

Under **Event Subscriptions**, subscribe to:

- `app_mention` — handled by `_mention` in `agent/slack_app.py`, runs at family
  priority and replies in the same thread.
- `message.im` — handled by `_dm`; the bot answers any direct message from a human.
- `message.channels` — also handled by `_dm`. In practice `should_ignore()` only
  lets DMs (`channel_type == "im"`) through; channel traffic in `#family` /
  `#homelab` is recorded implicitly through `app_mention` instead. Subscribe to it
  anyway so the bot can see it's a member of those channels and so a future change to
  `should_ignore()` doesn't need a new subscription.

No Slash Commands, Interactivity, or other subscriptions are used.

## 4. Bot loop guard (already in code, listed here for the setup checklist)

`should_ignore()` in `agent/slack_app.py` drops any `message` event that has a
`bot_id`, has `subtype == "bot_message"`, or isn't a DM. `_mention` also drops any
`app_mention` event carrying a `bot_id`. This stops the bot from replying to its own
`chat.postMessage` calls or to another bot's messages — without it, the bot posting to
a channel it's also listening on becomes an infinite reply loop.

## 5. Install the app and collect tokens

- Install the app to the workspace.
- Copy the bot token (`xoxb-...`) into `SLACK_BOT_TOKEN`.
- Copy the app-level token (`xapp-...`) from step 1 into `SLACK_APP_TOKEN`.
- Both are read from the environment at call time (`agent/config.py`,
  `agent/tools/comms.py`) — never commit them.

## 6. Create channels and invite the bot

- Create `#homelab`, `#family`, `#agent-log` if they don't exist.
- Invite the bot user to all three.
- Invite family members to `#family` only. `#homelab` and `#agent-log` are for the
  owner.
