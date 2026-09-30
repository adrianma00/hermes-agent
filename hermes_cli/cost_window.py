"""``hermes cost-window`` — inspect and bypass the provider peak-price gate.

Renders SGT (rule R23): every timestamp a human reads is labelled with the
+08:00 offset, never UTC.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8))
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# A bypass far enough out to read as "until I clear it" without being infinite;
# a past instant is expired, which is what makes the card self-clearing.
_INDEFINITE_SECONDS = 365 * 24 * 3600


def _sgt(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, SGT).strftime("%Y-%m-%d %H:%M:%S SGT")


def _parse_duration(raw: str | None) -> float:
    """'2h' / '90m' / '45s' / 'indefinite' -> seconds. Default 2h."""
    if not raw:
        return 2 * 3600
    text = str(raw).strip().lower()
    if text in {"indefinite", "forever", "always"}:
        return float(_INDEFINITE_SECONDS)
    unit = text[-1]
    number = text[:-1] if unit.isalpha() else text
    try:
        value = float(number)
    except ValueError:
        raise ValueError(f"unparseable duration {raw!r} (try 2h, 90m, 45s, or indefinite)")
    if value <= 0:
        raise ValueError(f"duration must be positive, got {raw!r}")
    if unit == "s" or unit.isdigit():
        return value
    if unit == "m":
        return value * 60
    if unit == "h":
        return value * 3600
    if unit == "d":
        return value * 86400
    raise ValueError(f"unknown unit in {raw!r} (use s, m, h, or d)")


def _fmt_windows(windows) -> str:
    if not windows:
        return "(none parsed — check kanban.cost_window.peak_windows_utc)"
    return ", ".join(f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')} UTC" for s, e in windows)


def _fmt_days(days) -> str:
    if not days:
        return "(none parsed)"
    return ",".join(_DAY_NAMES[d] for d in sorted(days))


def cost_window_command(args) -> int:
    from hermes_cli.kanban_cost_window import (
        read_cost_window_state, set_bypass, clear_bypass, _bypass_until,
        _cost_window_config,
    )

    action = getattr(args, "action", None)

    if action == "open":
        try:
            seconds = _parse_duration(getattr(args, "for_", None))
        except ValueError as exc:
            print(f"cost-window: {exc}")
            return 2
        until = time.time() + seconds
        try:
            set_bypass(until)
        except RuntimeError as exc:
            print(f"cost-window: {exc}")
            return 1
        print(f"✅ Cost-window bypass ACTIVE until {_sgt(until)}")
        print("   Cards that would have waited for off-peak now spawn immediately.")
        print("   The quota gate is NOT bypassed — use 'hermes quota open' for that.")
        return 0

    if action == "close":
        clear_bypass()
        state = read_cost_window_state()
        print("✅ Cost-window bypass cleared.")
        print(f"   {_status_line(state)}")
        return 0

    # status (default)
    state = read_cost_window_state()
    block = _cost_window_config()
    print("⏱️  Provider cost-window")
    print(f"   enabled        : {bool(block.get('enabled', False))}")
    print(f"   peak windows   : {_fmt_windows(state.windows)}")
    print(f"   peak days      : {_fmt_days(state.days)}")
    print(f"   priced providers: {', '.join(state.providers) or '(none)'}")
    _clock = {True: "synchronised (NTP)", False: "NOT synchronised", None: "unknown"}
    print(f"   clock           : {_clock.get(state.clock_synced, 'unknown')}")

    until = _bypass_until()
    if until:
        print(f"   bypass         : ACTIVE until {_sgt(until)}")
    print()
    print(f"   {_status_line(state)}")
    warning = _clock_warning(state)
    if warning:
        print(warning)

    if not bool(block.get("enabled", False)):
        print()
        print("   ℹ️  Disabled — dispatch is never deferred. Enable with:")
        print("      hermes config set kanban.cost_window.enabled true")
    return 0


def _status_line(state) -> str:
    if state.reason == "disabled":
        return "Status: DISABLED — cards spawn immediately."
    if state.reason == "bypass_active":
        return "Status: BYPASS — cards spawn immediately, peak-priced or not."
    if state.reason == "awaiting_ntp":
        until = f" (re-checking {_sgt(state.resume_at)})" if state.resume_at else ""
        return (
            "Status: HOLDING for clock sync — the system clock is not NTP-"
            "synchronised, and a time-based decision must not be made on a clock "
            f"known to be wrong{until}."
        )
    if state.is_deferred and state.resume_at:
        return (
            f"Status: DEFERRED (peak hours) — cards routed to "
            f"{', '.join(state.providers) or 'priced providers'} wait until "
            f"{_sgt(state.resume_at)}. Per-card override: "
            f"`hermes kanban run-now <id>`; fleet-wide: `hermes cost-window open 2h`."
        )
    return "Status: OFF-PEAK — cards spawn at the discounted rate."


def _clock_warning(state) -> str:
    """A separate line, because the window evaluation owns the status line.

    The grace-elapsed case still produces a normal deferred/off-peak verdict, so
    folding the warning into that string would erase it.
    """
    if getattr(state, "clock_grace_elapsed", False):
        return (
            "   ⚠️  Clock still unsynchronised past the grace period — proceeding "
            "anyway (fail open). Check this host's time source."
        )
    if state.clock_synced is False:
        return "   ⚠️  Clock is NOT NTP-synchronised."
    return ""
