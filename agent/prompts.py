"""Shared system-prompt text used by more than one system prompt.

Kept out of `agent/slack_app.py` and `agent/tick.py` themselves so the two
prompts (family chat, daemon tick) can't drift apart on wording that
matters - there is exactly one place that states what a VM 200 restart
costs, and both prompts quote it verbatim.
"""

# The owner gave the agent full administrative control over VM 200 (mt5,
# the MetaTrader trading VM) - see docs/homelab-agent-spec.md. This
# previously also carried a hard prohibition on placing/modifying/closing
# trades and on giving trading advice; the owner has since explicitly and
# repeatedly removed both restrictions and asked for a real route to trade.
# There is deliberately no trading-specific prohibition left here. What
# remains is operational knowledge, not a restriction: it makes the agent
# better at the job of administering this VM, the same way knowing a
# service's restart behaviour helps with any other guest.
MT5_GUARDRAILS = (
    "VM 200 (mt5) runs MetaTrader and is a guest like every other one now: you have "
    "full administrative control over it through the same tools - guests_list shows "
    "it, guest_action can start/stop/reboot it, and guest exec reaches it through the "
    "QEMU guest agent, the same as any other guest. Reading its account state - open "
    "positions, balance, equity, margin, trade history, EA status, journal entries - "
    "is expected and encouraged: report it plainly, the same as you'd report "
    "Jellyfin's library size or a disk's free space.\n"
    "One operational fact, not a restriction: stopping or rebooting VM 200 "
    "force-kills MetaTrader mid-session. Its running profile is not saved, only the "
    "terminal's startup-config Expert Advisor reattaches automatically on the next "
    "start, and anything attached to a chart by hand - an indicator, a manually-added "
    "EA, a template - is lost and does not come back on its own. Know that cost "
    "before you stop or reboot it. Immediately afterward, verify the result "
    "yourself: confirm the VM is back up and confirm whether MetaTrader and its "
    "startup EA actually reattached, then report exactly what you found - not just "
    "that the action you took succeeded."
)
