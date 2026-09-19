# Slack setup checklist

One-time manual setup at `https://api.slack.com/apps`, needed before the agent process
can run. Nothing here touches the repo or secrets. Do this once per workspace and hand
the resulting tokens to the deployment step.

Derived from what `agent/slack_app.py` and `agent/tools/comms.py` actually call, not
copied from the plan.

## How to talk to the agent

Talk to it in any channel it's a member of, public or private, or in a direct
message. No `@mention` needed, and there's no special "family channel": wherever you
talk to it, it answers in that same place and thread. Invite it to a channel and it
starts answering there.

If a channel gets noisy with the agent replying to things that weren't meant for it
(people chatting to each other, not the bot), set `SLACK_REPLY_WITHOUT_MENTION=0` in
the env file and restart the agent. With that set, channel messages need an
`@mention` to get a reply; direct messages never need one either way. See step 5.

The agent will discuss the trading VM (`mt5`) as infrastructure if it's in a channel
about it, for example whether it's running or how much memory it's using. It will not
give trading advice, and it has no ability to act on that VM at all: that's enforced
in the code, not just the prompt.

## 1. Create the app and enable Socket Mode

- Create a new app "from scratch" in the target workspace.
- Under **Socket Mode**, turn it on. This is what lets the agent run behind the
  Vodafone router with no inbound port forward: it dials out over a websocket
  instead of accepting webhooks.
- Generate an app-level token with the `connections:write` scope. This is the
  `xapp-` token (`SLACK_APP_TOKEN`).

## 2. Bot token scopes (OAuth & Permissions)

| Scope | Why |
|---|---|
| `channels:history` | read messages in public channels the bot is in (`message.channels`) |
| `channels:read` | list/resolve public channels, including for the startup preflight |
| `groups:history` | read messages in private channels the bot is in (`message.groups`) |
| `groups:read` | list/resolve private channels, including for the startup preflight |
| `chat:write` | `slack_say` posts via `chat.postMessage`, and `say()` replies in `handle_message` |
| `im:history` | read DM history (`message.im`) |
| `im:read` | list/open DM conversations |
| `im:write` | open a DM conversation with a user if needed |
| `users:read` | resolve user IDs to names for logging/replies |

**`groups:history` and `groups:read` are not yet granted on this app.** Everything
else in this table is. If the agent stays silent in a private channel it's been
invited to, this is why: add both scopes, then reinstall the app so the new scopes
take effect.

`files:write` from the original plan is dropped: nothing in `agent/tools/comms.py` or
`agent/slack_app.py` uploads a file. Add it back only if a future tool needs
`files.upload`.

`app_mentions:read` is also dropped. Earlier versions of this file listed it because
the code had a separate `app_mention` handler. That handler is gone: `agent/slack_app.py`
now has a single `message` event handler that answers DMs, plain channel messages, and
channel messages carrying a mention alike, so a message posted in a channel the bot is
in never needs the `app_mention` event to be understood. The one thing `app_mentions:read`
would still buy is being told about a mention in a channel the bot has *not* joined,
but the bot cannot reply there anyway (it isn't a member, so it has no history access
and posting into it needs an explicit join first), so subscribing to an event the code
has nothing useful to do with adds nothing. If a future requirement is "answer mentions
in channels the bot hasn't joined," that scope and a handler come back together.

## 3. Event subscriptions

Under **Event Subscriptions**, subscribe to:

- `message.im`: DMs. Every DM from a human gets a reply, no mention needed.
- `message.channels`: plain messages and mentions alike in public channels the bot
  has joined.
- `message.groups`: same, for private channels the bot has joined (needs
  `groups:history`, see step 2).

No `app_mention`, Slash Commands, Interactivity, or other subscriptions are used. See
the scopes table above for why `app_mention` was dropped.

## 4. Bot loop guard and mention behaviour (already in code, listed here for reference)

`should_ignore()` in `agent/slack_app.py` drops any `message` event that has a
`bot_id` or carries any `subtype` at all (edits, deletions, and other non-post
variants all set one; a genuine human post never does). This stops the bot from
replying to its own `chat.postMessage` calls or to another bot's messages. Without
it, the bot posting to a channel it's also listening on becomes an infinite reply
loop.

`needs_mention()` decides whether a message additionally needs an `@mention` before
it gets a reply: never for a DM, and for anything else (public channel or private
channel alike) it follows `SLACK_REPLY_WITHOUT_MENTION` (default on, see the env var
below).

## 5. `SLACK_REPLY_WITHOUT_MENTION` (optional env var)

- Default (`1`, or unset): plain messages in any channel the bot is in get a reply,
  the same as a DM. This is what "chat to the agent freely" means in practice.
- `0`: channel messages need an explicit `@mention` to get a reply. DMs are
  unaffected either way: a direct message is always answered.
- Read at call time from the environment, like every other setting in this
  codebase, so changing it just needs a value in `/etc/homelab-agent/env` and a
  restart, not a code change.

## 6. `SLACK_CHANNEL_STATUS` and `SLACK_CHANNEL_LOG` (optional env vars)

These are the only two channels the agent treats specially, and only as places it
posts to on its own initiative. They are not a gate on where it listens or replies:
that's every channel it's a member of, per step 3, with no allowlist.

| Env var | Default | Used for |
|---|---|---|
| `SLACK_CHANNEL_STATUS` | `#homelab-alerts` | what the agent did, incidents, the "online" line it posts on startup |
| `SLACK_CHANNEL_LOG` | `#homelab-agent-log` | the full tool-call audit trail |

Both are read once at process start, from the environment, the same way
`agent/config.py`'s settings are. Change either in `/etc/homelab-agent/env` and
restart to point them at different channels; no code change needed. This is
deliberate: hardcoded channel names (`#homelab`, `#family`, `#agent-log`) are what
broke the agent's startup announcement the first time this workspace's real channel
names turned out to be different (see step 7).

## 7. Startup channel preflight (already in code, no action needed)

`slack_app.preflight_channels()` checks the configured status and log channels
against the bot's own conversation list before the agent announces itself, and logs
one clear line per miss, for example:

```
configured status channel #homelab-alerts not found or bot not a member
```

This isn't fatal (a missing channel doesn't stop the agent from starting or from
answering elsewhere), but it means a channel-name mistake shows up immediately in
`journalctl` instead of as a silent `channel_not_found` nobody notices.

## 8. Install the app and collect tokens

- Install the app to the workspace.
- Copy the bot token (`xoxb-...`) into `SLACK_BOT_TOKEN`.
- Copy the app-level token (`xapp-...`) from step 1 into `SLACK_APP_TOKEN`.
- Both are read from the environment at call time (`agent/config.py`,
  `agent/tools/comms.py`). Never commit them.

## 9. Invite the bot to the channels it should answer in

The bot replies in every channel it's a member of, so membership is the only thing
that controls where it's active. Invite it to whichever public and private channels
it should see, including `#homelab-alerts` and `#homelab-agent-log` if those are the
channels in use for status and log posts (see step 6). The live workspace's channels
are `all-jmrsquared`, `homelab-alerts`, `homelab-income`, `homelab-media`,
`homelab-movies`, `homelab-mt5`, `homelab-net`, `homelab-notifications`,
`homelab-video-generation`, and `social`; invite it to whichever of these (or private
channels) it's meant to be part of.

## 10. Set the bot's display name to `@homelab-bot`

- In the app config, go to **App Home**.
- Under "Your App's Presence in Slack", edit the bot user's **Display Name** and
  **Default username** to `homelab-bot`.
- The bot is currently named `homelab` (workspace Jmrsquared, confirmed live via
  `auth.test`). Renaming it only relabels how it appears; it keeps the same user id.
  Nothing in this repo reads or depends on the display name, so this step is
  cosmetic and safe to do any time, including well after everything else here is
  done. Old `@homelab` mentions stop autocompleting once renamed, but nothing
  breaks.
