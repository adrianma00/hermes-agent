"""Provider cost-window gate: defer dispatch out of a provider's PEAK-price hours.

DeepSeek bills by the UTC clock (since 2026-08-16): peak is 01:00-04:00 and
06:00-10:00 UTC, Monday-Friday **excluding Chinese public holidays** (the
provider revised the terms on 2026-10-04), and every other hour — all weekend
AND those holidays in full — is off-peak at exactly half price. Same tokens,
same model — only the arrival time differs. That makes *when* the dispatcher
spawns a card a pricing decision, and a fleet that dispatches the instant a card
is created pays double for no benefit on any work that is not latency-critical.

The holiday carve-out is honoured ONLY from an explicitly configured snapshot, and
fail-safe by construction. This module owns no calendar of its own: it reads
``kanban.cost_window.holiday_calendar`` (a JSON snapshot produced by
``~/.hermes/scripts/holiday_calendar_refresh.py``) and treats a date as a holiday
only when the file parses, its ``valid_through`` has not passed, and the snapshot
covers that year. Any doubt means PEAK.

That direction is the whole point. A carve-out can only REMOVE a deferral, so
believing a holiday that is not one dispatches INTO peak at double price, while
ignoring a real one costs latency only — on batch work this gate is already
delaying by design. Default is ``off`` (no snapshot, today's behaviour); an
expired or corrupt snapshot therefore degrades to a little unnecessary deferral
and can never spend money. The refresher is the loud half: it warns while
coverage is still current, so this never silently switches itself off in January.

This module is deliberately the twin of the provider-quota gate
(``read_quota_gate_state``) with one structural difference that shapes the whole
design: the quota wall quotes ONE reset instant and is written to a shared board
card, whereas a cost window is a RECURRING daily boundary that every install can
compute from the clock alone. So there is no gate card, no cross-install state to
reconcile, and no reset bookkeeping — the dispatcher simply re-evaluates each
tick and spawns the moment the window ends.

Scope is deliberately narrow:

- **Only priced providers.** ``custom`` (a flat-rate coding plan) has no
  time-of-day pricing, so the clock is irrelevant to it. The window applies only
  when the card's effective provider is in ``providers`` (default: deepseek).
- **Never interactive chat.** Only kanban dispatch and cron consult this. Both read it
  for work they are about to run on a priced provider — including work the quota gate has
  RELEASED onto the metered fallback, because releasing a card or job authorises the spend
  without licensing the peak rate.
- **Defer, don't fail.** A deferred card is untouched and ready; it spawns
  unchanged when the window ends. ``run_now`` on the card (or on the cron job) forces it
  immediately.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

# Peak windows in UTC, as the provider publishes them. Overridable per install.
DEFAULT_PEAK_WINDOWS_UTC = ("01:00-04:00", "06:00-10:00")
# Where the Chinese public-holiday snapshot lives. Empty = the carve-out is not
# honoured (every weekday defers). Any path is used ONLY while provably current:
# see ``holiday_state`` — an unusable snapshot means peak, never a guess.
DEFAULT_HOLIDAY_CALENDAR = ""
# Peak days. DeepSeek's documented rule is Mon-Fri EXCLUDING Chinese public
# holidays; weekends are entirely off-peak. Configurable because a provider can
# change this. The holiday carve-out is NOT encoded here — it comes from the
# ``holiday_calendar`` snapshot, so a calendar that goes stale degrades to peak
# instead of silently disabling the deferral. See
# the module docstring — it can only remove a deferral, so getting it wrong pays
# peak price, while ignoring it only defers on a day that was off-peak anyway.
DEFAULT_PEAK_DAYS = ("mon", "tue", "wed", "thu", "fri")
# Providers whose pricing varies by time of day. A flat-rate provider (a coding
# plan) must NOT be listed: deferring it would cost time and save nothing.
DEFAULT_PROVIDERS = ("deepseek",)

_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
              "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
              "friday": 4, "saturday": 5, "sunday": 6}

_BYPASS_TITLE = "cost-window:bypass"


def _parse_hhmm(value: str) -> Optional[dtime]:
    """``"HH:MM"`` -> ``datetime.time`` in UTC, or None when malformed."""
    try:
        hh, _, mm = str(value).strip().partition(":")
        h, m = int(hh), int(mm or 0)
    except (TypeError, ValueError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return dtime(hour=h, minute=m)


def parse_windows_utc(windows) -> list[tuple[dtime, dtime]]:
    """``["01:00-04:00", …]`` -> ``[(start, end), …]``. Malformed entries dropped."""
    out: list[tuple[dtime, dtime]] = []
    for raw in windows or ():
        start_s, _, end_s = str(raw).partition("-")
        start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
        if start is None or end is None or start == end:
            continue
        out.append((start, end))
    return out


def parse_days(days) -> set[int]:
    """``["mon", …]`` -> ``{0, …}`` (Monday=0). Malformed entries dropped."""
    out: set[int] = set()
    for raw in days or ():
        idx = _DAY_INDEX.get(str(raw).strip().lower())
        if idx is not None:
            out.add(idx)
    return out


def _in_window(now: dtime, start: dtime, end: dtime) -> bool:
    """Whether ``now`` falls in ``[start, end)``; handles a window spanning midnight."""
    if start <= end:
        return start <= now < end
    # e.g. 23:00-01:00 wraps through midnight.
    return now >= start or now < end


def find_peak_window(
    now_utc: datetime,
    windows: list[tuple[dtime, dtime]],
    days: set[int],
) -> Optional[tuple[dtime, dtime]]:
    """The peak window containing ``now_utc``, or None when off-peak.

    A window may cross midnight, in which case the day that *starts* it is the
    one gating membership: a wrap window listed on Monday covers Monday 23:00
    and the small hours of Tuesday.
    """
    if not windows:
        return None
    for start, end in windows:
        if start <= end:
            if now_utc.weekday() in days and _in_window(now_utc.time(), start, end):
                return (start, end)
        else:
            # Wrapping window: the pre-midnight leg is gated by today, the
            # post-midnight leg by yesterday.
            t = now_utc.time()
            if (t >= start and now_utc.weekday() in days) or (
                t < end and (now_utc.weekday() - 1) % 7 in days
            ):
                return (start, end)
    return None


def next_window_end(
    now_utc: datetime,
    windows: list[tuple[dtime, dtime]],
    days: set[int],
) -> Optional[datetime]:
    """When the peak window containing ``now_utc`` ends (None when off-peak)."""
    window = find_peak_window(now_utc, windows, days)
    if window is None:
        return None
    _, end = window
    candidate = now_utc.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= now_utc:
        # Wrapped past midnight, or the end lands in the next day.
        candidate += timedelta(days=1)
        candidate = candidate.replace(hour=end.hour, minute=end.minute)
    return candidate


@dataclass
class CostWindowState:
    """Whether dispatch is currently deferred for a priced provider."""

    # True when cards routed to this provider must WAIT (we are inside peak).
    is_deferred: bool = False
    # When the deferral ends (only meaningful while ``is_deferred``).
    resume_at: Optional[float] = None
    # The provider the deferral applies to, when scoped.
    provider: Optional[str] = None
    # Peak windows in force, for reporting.
    windows: list[tuple[dtime, dtime]] = field(default_factory=list)
    days: set[int] = field(default_factory=set)
    # A short machine-readable reason (``peak_hours`` / ``bypass_active`` / ``disabled``).
    reason: str = "disabled"
    # Providers the deferral is configured for (populated by the reader).
    providers: tuple[str, ...] = ()
    # Clock trust: True/False from timedatectl, None when undeterminable.
    # Reported so an operator can see WHY a deferral is happening — a hold for
    # "awaiting_ntp" is a different story from a hold for peak pricing.
    clock_synced: Optional[bool] = None
    # The clock stayed unsynchronised past the grace period and we proceeded
    # anyway. Its own field, not a ``reason``: the window evaluation still runs
    # and owns ``reason``, so folding the warning in there would erase it.
    clock_grace_elapsed: bool = False
    # Holiday carve-out (opt-in via ``holiday_calendar``): the configured snapshot's
    # usability and, when today is one, the holiday's name. ``holiday_reason`` says
    # why an unusable snapshot was refused, so the status line can explain itself.
    holiday_calendar: str = ""
    holiday_usable: bool = False
    holiday_valid_through: str = ""
    holiday_reason: str = ""
    holiday_off_days: int = 0
    holiday_today: str = ""

    def covers(self, provider: Optional[str]) -> bool:
        """Whether this deferral applies to a card running on ``provider``.

        ``None`` means "unknown provider" — treated as covered, because the
        dispatcher's default route is the priced fallback. A card explicitly
        pinned to a provider outside the configured set is NOT covered (a flat-
        rate coding plan must never be deferred to save nothing).
        """
        if not self.is_deferred:
            return False
        if provider is None:
            return True
        slug = str(provider).strip().lower()
        if not slug:
            return True
        return slug in {str(p).strip().lower() for p in (self.providers or ())}


def _cost_window_config() -> dict:
    """The ``kanban.cost_window`` config block, or {} when unavailable."""
    try:
        from hermes_cli.config_effective import load_user_config_effective

        cfg = load_user_config_effective() or {}
        block = (cfg.get("kanban") or {}).get("cost_window") or {}
        return block if isinstance(block, dict) else {}
    except Exception:
        _log.debug("cost-window config unavailable", exc_info=True)
        return {}


# ── Holiday carve-out (opt-in, fail-safe) ───────────────────────────────
# The provider's peak days are Mon-Fri EXCLUDING Chinese public holidays. This module
# keeps no calendar, so the carve-out is honoured only from a snapshot the operator
# pointed at — and only while that snapshot is readable, unexpired and covers the date.
_holiday_cache: dict = {"key": None, "data": None}
# China Standard Time. The peak WINDOWS are UTC, but both of them sit inside one
# Beijing day (09:00-12:00 and 14:00-18:00 CST), so the holiday's date is unambiguous.
# No DST in CST, so a fixed offset is exact rather than an approximation.
_BEIJING = timezone(timedelta(hours=8))


def beijing_date(moment: datetime) -> str:
    """The ISO date the provider's holiday rule refers to, for ``moment``."""
    return moment.astimezone(_BEIJING).date().isoformat()


def _holiday_snapshot(path: str) -> tuple[Optional[dict], str]:
    """``(snapshot, error)`` — the parsed file, cached on (path, mtime, size).

    The dispatcher asks on every tick, so this must not re-read a 5 KB JSON
    constantly; it must also not serve a stale answer after the refresher rewrites
    the file, hence the mtime+size key rather than a TTL.
    """
    try:
        stat = os.stat(path)
    except OSError as exc:
        return None, f"unreadable ({exc.strerror or exc})"
    key = (path, stat.st_mtime, stat.st_size)
    if _holiday_cache.get("key") != key:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            _holiday_cache["key"], _holiday_cache["data"] = key, None
            return None, f"unparseable ({exc})"
        _holiday_cache["key"], _holiday_cache["data"] = key, data
    data = _holiday_cache.get("data")
    if not isinstance(data, dict):
        return None, "unparseable"
    return data, ""


def holiday_state(block: dict, moment: datetime) -> dict:
    """Whether the holiday snapshot may be trusted, and whether TODAY is on it.

    Fail-safe by construction — ``usable`` is False for an absent, unreadable,
    unparseable, expired, or year-not-covered snapshot, and the caller then treats
    the day as a normal weekday. A wrong answer in THAT direction costs latency; a
    wrong answer the other way would dispatch into peak at double price.
    """
    path = str(block.get("holiday_calendar") or DEFAULT_HOLIDAY_CALENDAR).strip()
    out = {"path": path, "usable": False, "valid_through": "", "when": "",
           "name": "", "reason": "", "off_days": 0}
    if not path or path.lower() in {"off", "none", "disabled", "false", "0"}:
        out["reason"] = "off"
        return out
    snapshot, error = _holiday_snapshot(path)
    if error or not snapshot:
        out["reason"] = error or "unreadable"
        return out
    when = beijing_date(moment)
    out["when"] = when
    valid_through = str(snapshot.get("valid_through") or "")
    out["valid_through"] = valid_through
    if not valid_through:
        out["reason"] = "snapshot has no valid_through"
        return out
    if when > valid_through:
        out["reason"] = f"snapshot expired {valid_through}"
        return out
    years = snapshot.get("years")
    year = years.get(when[:4]) if isinstance(years, dict) else None
    if not isinstance(year, dict):
        out["reason"] = f"{when[:4]} not covered by the snapshot"
        return out
    holidays = year.get("holidays") or {}
    out["usable"] = True
    out["off_days"] = len(holidays)
    out["name"] = str(holidays.get(when) or "")
    return out


def _bypass_until(now: Optional[float] = None) -> Optional[float]:
    """Epoch a fleet-wide bypass expires, or None when no bypass is active.

    Phase 2A: reads from the local state DB instead of a shared-board card.
    The bypass is per-install (the cost-window config — peak hours, holiday
    calendar, pricing — remains in config.yaml as policy).

    ``now`` is injectable so the whole reader can be evaluated at an arbitrary
    time; it defaults to the real clock.
    """
    moment = now if now is not None else time.time()
    try:
        from hermes_cli.quota_state import read_bypass as _read_bypass
        return _read_bypass()
    except Exception:
        _log.debug("cost-window bypass read failed", exc_info=True)
        return None


# ── Clock trust ─────────────────────────────────────────────────────────
# This gate makes a decision FROM the clock, so an unsynchronised clock is not
# a detail — it is the input. This host boots with its RTC ~3.3h fast and NTP
# steps the clock back seconds later (journal: "rtc_cmos 00:03: setting system
# clock to 2026-09-29T21:49:55 UTC"), so for those first seconds every
# time-based decision is made on a value known to be wrong.
#
# The response is to WAIT, not to guess: hold cards for a moment until the clock
# is trustworthy. Bounded, because a host with no NTP at all must never park the
# fleet forever — after the grace period we proceed (fail open, like everything
# else here).
_CLOCK_SYNC_TTL = 5.0          # seconds to reuse one timedatectl answer
_CLOCK_SYNC_UNKNOWN = object()  # "not measured yet" — distinct from a real None
_clock_sync_cache: dict = {"at": None, "synced": _CLOCK_SYNC_UNKNOWN}
_clock_unsynced_since: Optional[float] = None
# How long to keep asking for NTP before giving up and proceeding.
DEFAULT_CLOCK_SYNC_GRACE_SECONDS = 300.0
# Re-check cadence while waiting (and the resume instant we publish).
_CLOCK_RETRY_SECONDS = 30.0


def _ntp_synchronized(now: Optional[float] = None) -> Optional[bool]:
    """Whether the system clock is NTP-synchronised: True / False / None.

    ``None`` means undeterminable (no ``timedatectl``, non-systemd host, probe
    error) and callers must treat it as "cannot prove it is wrong" — i.e. proceed.
    Cached briefly so a tick that evaluates many cards shells out once.
    """
    moment = now if now is not None else time.time()
    cached_at, cached = _clock_sync_cache.get("at"), _clock_sync_cache.get("synced")
    if cached is not _CLOCK_SYNC_UNKNOWN and cached_at is not None and moment - cached_at < _CLOCK_SYNC_TTL:
        return cached
    synced: Optional[bool] = None
    try:
        proc = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            synced = proc.stdout.strip().lower() in {"yes", "true", "1"}
    except Exception:
        synced = None
    _clock_sync_cache["at"], _clock_sync_cache["synced"] = moment, synced
    return synced


def _reset_clock_cache() -> None:
    """Clear the cached sync answer and the unsynced-since marker (tests)."""
    global _clock_unsynced_since
    _clock_sync_cache["at"] = None
    _clock_sync_cache["synced"] = _CLOCK_SYNC_UNKNOWN
    _clock_unsynced_since = None


def read_cost_window_state(
    provider: Optional[str] = None,
    *,
    enabled: Optional[bool] = None,
    now: Optional[float] = None,
) -> CostWindowState:
    """Whether dispatch to ``provider`` should wait for an off-peak window.

    Opt-in via ``kanban.cost_window.enabled`` (default **false**): an install
    that never opted in dispatches exactly as before. Unlike the quota gate this
    one **fails OPEN** — a clock/config problem must not stop the fleet, because
    the cost of getting it wrong is money, not a wall of failed spawns.

    ``enabled`` overrides the config (used by tests and the CLI).
    """
    block = _cost_window_config()
    if enabled is None:
        enabled = bool(block.get("enabled", False))

    # Parse the schedule BEFORE the disabled check so `cost-window status` can
    # report what WOULD apply — an operator reading "disabled" still needs to see
    # the windows it would use.
    windows = parse_windows_utc(block.get("peak_windows_utc") or DEFAULT_PEAK_WINDOWS_UTC)
    days = parse_days(block.get("peak_days") or DEFAULT_PEAK_DAYS)
    providers = tuple(
        str(p).strip().lower()
        for p in (block.get("providers") or DEFAULT_PROVIDERS)
        if str(p).strip()
    )
    require_clock_sync = bool(block.get("require_clock_sync", True))
    try:
        grace = float(block.get("clock_sync_grace_seconds", DEFAULT_CLOCK_SYNC_GRACE_SECONDS))
    except (TypeError, ValueError):
        grace = DEFAULT_CLOCK_SYNC_GRACE_SECONDS

    if not enabled:
        return CostWindowState(windows=windows, days=days, providers=providers, reason="disabled")

    state = CostWindowState(windows=windows, days=days, providers=providers)

    moment = datetime.fromtimestamp(now, timezone.utc) if now is not None else datetime.now(timezone.utc)

    # Bypass is evaluated at the SAME instant as the windows, so an injected
    # clock governs both (and a test can reason about the pair).
    until = _bypass_until(now=moment.timestamp())
    if until:
        state.reason = "bypass_active"
        return state

    # Never decide a TIME-based question from a clock we know is wrong. Hold
    # cards until NTP lands, bounded by the grace period so an NTP-less host
    # still makes progress.
    if require_clock_sync:
        synced = _ntp_synchronized(moment.timestamp())
        state.clock_synced = synced
        if synced is False:
            global _clock_unsynced_since
            if _clock_unsynced_since is None:
                _clock_unsynced_since = moment.timestamp()
            waited = moment.timestamp() - _clock_unsynced_since
            if waited < grace:
                state.is_deferred = True
                state.resume_at = moment.timestamp() + _CLOCK_RETRY_SECONDS
                state.reason = "awaiting_ntp"
                return state
            # NTP is not coming (no network, no daemon, late boot). Fail open:
            # a wrong-but-progressing fleet beats a permanently parked one.
            state.clock_grace_elapsed = True
        else:
            _clock_unsynced_since = None

    # The carve-out can only REMOVE a deferral, so it is answered before the window:
    # on a Chinese public holiday there is no peak to wait out, and asking the window
    # first would return a deferral that has to be undone.
    holiday = holiday_state(block, moment)
    state.holiday_calendar = holiday["path"]
    state.holiday_usable = bool(holiday["usable"])
    state.holiday_valid_through = str(holiday["valid_through"] or "")
    state.holiday_reason = str(holiday["reason"] or "")
    state.holiday_off_days = int(holiday["off_days"] or 0)
    if holiday["usable"] and holiday["name"]:
        state.holiday_today = str(holiday["name"])
        state.reason = "off_peak_holiday"
        return state

    resume = next_window_end(moment, windows, days)
    if resume is None:
        state.reason = "off_peak"
        return state

    state.is_deferred = True
    state.resume_at = resume.timestamp()
    state.reason = "peak_hours"
    return state


def is_priced_provider(provider: Optional[str]) -> bool:
    """Whether ``provider`` is one the window is configured for (no clock read)."""
    block = _cost_window_config()
    providers = {
        str(p).strip().lower()
        for p in (block.get("providers") or DEFAULT_PROVIDERS)
        if str(p).strip()
    }
    if provider is None:
        return bool(providers)
    return str(provider).strip().lower() in providers


def set_bypass(until_epoch: float) -> None:
    """Write/extend the cost-window bypass in the local state DB."""
    from hermes_cli.quota_state import write_bypass as _write_bypass

    _write_bypass(float(until_epoch))


def clear_bypass() -> None:
    """Drop the cost-window bypass (idempotent)."""
    from hermes_cli.quota_state import clear_bypass as _clear_bypass

    _clear_bypass()
