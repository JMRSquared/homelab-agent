---
name: mt5-trading-vm
description: VM 200, MetaTrader 5 and the TazzieMoney EA - account facts, how Algo Trading is enabled, the no-workarounds rule, panic, restarts, and where every TazzieBot report comes from.
---

# MT5 trading VM (VM 200)

Windows 11 at 10.0.0.171. Runs MetaTrader 5 with the TazzieMoney Expert Advisor.

## The rule that overrides everything below

The owner deleted every workaround that re-enables Algo Trading or trading on 2026-10-09. Never press keys on VM 200: no `qm sendkey`, no Ctrl+E, no tool or script that does either. Never write, restore or improve a heal script, cron or timer. If Algo Trading is off, alert once in #homelab-mt5 and find the cause in the MT5 journal. Brain topic `mt5 no workarounds directive 2026-10-09` has the full history.

## Facts

- Broker: Pepperstone demo account 61579976, server Pepperstone-Demo. Demo money.
- EA: TazzieMoney on ETHUSD M30, preset `Tazzie\TazzieMoney-eth-steady.set`, magic 20260830.
- Behaviour: trades on roughly a quarter of days, no new entries after 18:00 UTC, flat at UTC midnight. A day with no trades is normal.
- Source: the owner's repo TazzieBot (apps/metatrader5/MQL5), not on this box.

## How MT5 starts, and why that is the only thing that enables trading

1. Windows auto-logs in as `trader`.
2. Scheduled task `MT5-Watchdog` (SYSTEM, every 60s) starts scheduled task `MT5-Launch` when terminal64.exe is not running.
3. `MT5-Launch` runs `C:\Users\Public\mt5-launch.cmd`: it prunes old charts with `mt5-prune-charts.ps1`, then runs `terminal64.exe /skipupdate /config:C:\Users\Public\mt5-start.ini`.
4. `mt5-start.ini` enables algo trading (`[Experts] Enabled=1`, `AllowLiveTrading=1`) and its `[StartUp]` section opens ETHUSD M30 with template `TazzieMoney` (which carries the TazzieMoneySignals indicator) and attaches TazzieMoney.

So a restart through `MT5-Launch` brings back the EA, the indicator and Algo Trading with nobody touching anything. Starting terminal64.exe by hand with no arguments brings back none of them.

## Reading state

Ground truth is the terminal journal: `C:\Users\trader\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\logs\YYYYMMDD.log`. Look for `expert TazzieMoney (ETHUSD,M30) loaded successfully` and the last `automated trading is enabled` / `disabled` line. TazzieMoney also writes `trading ENABLED` to `MQL5\logs\YYYYMMDD.log` when it loads.

Algo Trading button polarity, measured against that journal: GREEN ARROW = enabled, RED SQUARE = disabled. Older notes said the opposite and were wrong.

`mt5_status` `hb_age_s` is the age of `TazzieOpsHeartbeat.csv`, which TazzieSpreadLogger writes. TazzieSpreadLogger is not attached, so `hb_age_s` says nothing about TazzieMoney. Do not tell the owner TazzieMoney is frozen because of it.

Host files: /tank/dev/tazzie-daily/YYYY-MM-DD.log (hourly, observation only), /tank/dev/tazzie-metrics/.

## Emergency stop

Slack `panic` writes /tank/dev/tazzie-PANIC and runs `qm suspend 200`, freezing MetaTrader without closing positions. The Proxmox hookscript `local:snippets/mt5-panic-guard.sh` refuses to boot VM 200 while that marker exists. `unpanic` removes the marker and resumes or starts the VM. Neither presses keys. If the marker exists, leave trading alone.

## Restarts

Do not restart VM 200 or MT5 unless the owner asks or MT5 is plainly down. A restart interrupts trade management for a minute or two; it does not lose the EA. After any restart, confirm from the journal that TazzieMoney loaded and algo is enabled, take `mt5_screenshot`, and report what you saw.

## Who posts what

The hourly status card in #homelab-mt5, trade alerts, and the daily P&L come from legacy host scripts, not from you. See skill `legacy-host-bot`. The daily P&L falls back to an equity delta when the trades table is empty; that number is not proof a trade happened.

## Quoting

Nested quoting through tool -> hostctl -> qm guest exec -> cmd/PowerShell mangles. For anything longer than one simple command, write a script file on the host, push it in base64, and run PowerShell with forward-slash paths.
