---
name: dns-adguard
description: AdGuard Home DNS filtering, which devices use it, and how to handle "the internet is down", "a site is blocked", or ads getting through.
---

# DNS: AdGuard Home

Web UI http://10.0.0.165:8080, DNS on 10.0.0.165:53. Stack `adguard`, container `adguardhome`, in LXC 101.

## Who uses it

The Vodafone router's firmware locks its DNS settings, so AdGuard cannot serve the whole house. Devices opt in one at a time:
- The owner's Mac and the Xbox: DNS set by hand to 10.0.0.165.
- Phones: through Tailscale's DNS override.
- Everything else uses Vodafone's DNS and bypasses AdGuard.

## "The internet is down"

AdGuard only affects the devices listed above. Work out the scope first.
1. `adguard_report`. If it answers with fresh query counts, AdGuard is fine.
2. If AdGuard is down, the Mac, Xbox and phones on Tailscale lose name resolution while other devices keep working. Restart the `adguard` stack with `docker_action`, then run `adguard_report` again.
3. If every device is offline, the fault is the router or Vodafone's line. You cannot fix that. Tell them to power-cycle the router and check Vodafone's outage page.
4. `host_exec` `ping -c3 1.1.1.1` and `ping -c3 google.com` separates "no internet" from "no DNS".

A quick workaround while you fix AdGuard: set the device's DNS to automatic, or switch Tailscale's DNS override off.

## "Site X is blocked"

You do not hold the AdGuard admin password, so you cannot read the query log or change filters. Confirm the device uses AdGuard (see the list above), then tell the owner which domain to allowlist under Filters > Custom filtering rules, written as `@@||example.com^`. Never disable filtering for the whole house to fix one site.

## Other

netwatch (/opt/homelab/netwatch.py on the host, every 10 minutes) logs connectivity to /tank/dev/netwatch/netwatch.log and posts to #homelab-net. Read it for outage history: `host_exec` `tail -50 /tank/dev/netwatch/netwatch.log`.
