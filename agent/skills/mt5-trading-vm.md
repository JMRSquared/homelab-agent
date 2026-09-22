---
name: mt5-trading-vm
description: VM 200, MetaTrader 5 and the TazzieMoney EA - account facts, heartbeats, the Algo Trading toggle, the heal script, restart costs, and where every TazzieBot report comes from.
---

# MT5 trading VM (VM 200)

Windows 11 at 10.0.0.171, 4GB RAM. Runs MetaTrader 5 with the TazzieMoney Expert Advisor.

## Facts

- Broker: Pepperstone demo account 61579976, server Pepperstone-Demo. Demo money.
- EA: TazzieMoney on ETHUSD M30, preset TazzieMoney-forward-demo.set, magic 20260830.
- Behaviour: trades on roughly a quarter of days, no new entries after 18:00 UTC, flat at UTC midnight. A day with no trades is normal.
- Source: the owner's repo TazzieBot (apps/metatrader5/MQL5), not on this box.

## Reading state

1. `mt5_status`. Two ages matter: `age_s` (account snapshot, fresh even with a frozen EA) and `hb_age_s` (the EA's own heartbeat). `hb_age_s` over ~5 minutes means the EA is not running, whatever the other numbers say.
2. `mt5_screenshot` shows the screen. It captures whatever tab MT5 shows; it cannot click.
3. Files on the host: /tank/dev/tazzie-metrics/ (tazzie.db, status.log, export.log, pnl.log, ocr.log), /tank/dev/tazzie-daily/YYYY-MM-DD.log (hourly health), /tank/dev/tazzie-daily/heal.log, /tank/dev/tazzie-alerts/.
4. Inside the VM: `guest_exec` 200 with cmd.exe syntax. Watchdog heartbeat: `type C:\Users\Public\mt5-watchdog-heartbeat.json`.

Read brain topics `MT5 stale-data trap` and `channel-homelab-mt5` too. They hold the latest known state and the reply shape the owner wants in #homelab-mt5.

## Algo Trading toggle

The toolbar's Algo Trading button must be ON (red square icon). With a green arrow the EA loads but cannot trade, and nothing on disk shows it. Only a screen read tells you. Ctrl+E toggles it. The keyboard reaches MT5 through `qm sendkey`; the mouse does not.

/root/tazzie-heal.sh on the host runs every 2 minutes. It screendumps the VM, reads the toggle's pixel colour, and presses Ctrl+E (verify, then press, up to 6 times) when it is off. Its log is /tank/dev/tazzie-daily/heal.log. Check that log before you touch the toggle yourself, so the two of you do not toggle it back and forth.

If /tank/dev/tazzie-PANIC exists, the owner has hit the panic switch through the legacy bot. Leave trading alone until they remove it.

## Restarts cost something

Stopping or rebooting VM 200 force-kills MetaTrader. Session 0 cannot close a session 1 window, so the profile never saves. Only the EA in the startup config reattaches, and on 2026-09-22 the saved chart profiles held no EA at all. A reboot may leave nothing trading. Do not suggest a reboot unless the owner asks for one. After any restart: confirm the VM is up, confirm terminal.exe is running (`tasklist | findstr terminal`), check `mt5_status` `hb_age_s` falls, take `mt5_screenshot`, and report what you saw.

Watchdog scheduled tasks inside the VM, `MT5-Watchdog` (every 60s) and `MT5-Launch`, restart the terminal if it dies.

## Who posts what

The hourly status card in #homelab-mt5, trade alerts, and the daily P&L all come from legacy host scripts, not from you. See skill `legacy-host-bot`. The daily P&L falls back to an equity delta when the trades table is empty; that number is not proof a trade happened.

## Quoting

Nested quoting through tool -> hostctl -> qm guest exec -> cmd/PowerShell mangles. For anything longer than one simple command, write a script file on the host, push it in base64, and run PowerShell with forward-slash paths.
