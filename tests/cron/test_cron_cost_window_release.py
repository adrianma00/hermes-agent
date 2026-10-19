"""Release-all must not buy the peak rate: a released cron job still observes the cost window.

`hermes quota release-all on` is the operator saying "this reset is too far away to wait
for — spend the fallback instead". Spending the fallback is a PRICED decision, and the fallback
is a metered provider with peak and off-peak hours (see `kanban_cost_window`). So releasing a
job authorises the spend; it must not license paying double for the same tokens. The kanban
dispatcher already holds a released CARD for the fallback's discount window, and the two work
sources that share one gate card must not disagree about when the cheap hours are.

These tests pin that parity, plus the two refusals that keep it honest:

  * a `no_agent` script job makes no model call, so there is no price to wait out; and
  * a job carrying `run_now` forces the run, exactly as the per-card flag does on the board.

The reader FAILS OPEN, so the last test pins that a broken clock/config returns a
non-deferring state rather than parking the fleet.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from cron import scheduler_quota as sq
from hermes_cli.kanban_db import QuotaGateState

WALLED = "custom"
FALLBACK = ("deepseek", "deepseek-v4-flash")


def _gate(release_all: bool = True, *, route: bool = True, reset_in: float = 6 * 86400):
    """A closed gate; ``release_all`` defaults ON — the state under test here."""
    return QuotaGateState(
        is_closed=True,
        reset_at=time.time() + reset_in,
        walled_provider=WALLED,
        window="monthly",
        fallback_model=FALLBACK[1] if route else None,
        fallback_provider=FALLBACK[0] if route else None,
        release_all=release_all,
    )


def _job(**over):
    job = {
        "id": "j1",
        "name": "nightly",
        "execution_id": "e1",
        "schedule": {"kind": "cron", "expr": "0 3 * * *"},
        # ``provider`` is absent by default: the job runs on whatever the gate routes it to.
    }
    job.update(over)
    return job


class _PeakState:
    """A minimal stand-in for ``CostWindowState`` inside peak hours on ``deepseek``."""

    def __init__(self, *, deferred: bool = True, resume_in: float = 3600.0):
        self.is_deferred = deferred
        self.resume_at = time.time() + resume_in
        self.reason = "peak_hours"
        self._providers = ("deepseek",)  # the metered fallback

    def covers(self, provider):
        from hermes_cli.kanban_cost_window import CostWindowState

        return CostWindowState(
            is_deferred=self.is_deferred, providers=self._providers, reason=self.reason,
        ).covers(provider)


@pytest.fixture
def peak(monkeypatch):
    """Make the cost window report: inside peak on the metered fallback."""
    state = _PeakState()
    monkeypatch.setattr(
        "hermes_cli.kanban_cost_window.read_cost_window_state", lambda **kw: state,
        raising=True,
    )
    return state


@pytest.fixture
def off_peak(monkeypatch):
    """Make the cost window report: not deferred (off-peak, or the gate is disabled)."""
    state = _PeakState(deferred=False)
    monkeypatch.setattr(
        "hermes_cli.kanban_cost_window.read_cost_window_state", lambda **kw: state,
        raising=True,
    )
    return state


# ── the release still waits for the cheap hours ────────────────────────────


def test_released_job_is_held_for_the_discount_window_when_the_fallback_is_at_peak(peak):
    """The gap this closes: released, but still deferred out of peak price."""
    job = _job()

    fire, held = sq.hold_jobs([job], _gate(True), now=time.time())

    assert fire == []
    assert held == [job]


def test_released_job_fires_once_the_discount_window_is_open(off_peak):
    """Off-peak: the release does what it says — the job runs."""
    job = _job()

    fire, held = sq.hold_jobs([job], _gate(True), now=time.time())

    assert fire == [job]
    assert held == []


def test_the_cost_window_is_not_consulted_when_the_gate_is_in_hold_mode(off_peak, monkeypatch):
    """HOLD mode holds the job for the quota reset; the price never enters into it."""
    called = []
    monkeypatch.setattr(
        "hermes_cli.kanban_cost_window.read_cost_window_state",
        lambda **kw: called.append(1) or _PeakState(),
        raising=True,
    )

    fire, held = sq.hold_jobs([_job()], _gate(False), now=time.time())

    assert fire == []
    assert len(held) == 1
    assert called == [], "the cost window must not be read for a job the gate never released"


def test_a_per_job_override_also_observes_the_cost_window(peak):
    """The legacy authorisation routes to the SAME metered fallback, so it defers too."""
    job = _job(quota_override=True)

    fire, held = sq.hold_jobs([job], _gate(False), now=time.time())

    assert fire == []
    assert held == [job]


def test_no_route_means_the_job_is_still_held_by_the_gate(peak):
    """Nowhere to route = the quota hold applies; the cost window is moot."""
    job = _job()

    fire, held = sq.hold_jobs([job], _gate(True, route=False), now=time.time())

    assert fire == []
    assert held == [job]


# ── the two refusals ──────────────────────────────────────────────────────


def test_a_script_job_is_never_deferred_for_price(peak):
    """A `no_agent` job spends no model tokens, so there is no price to wait out."""
    job = _job(no_agent=True)

    fire, held = sq.hold_jobs([job], _gate(True), now=time.time())

    assert fire == [job]
    assert held == []


def test_run_now_forces_a_released_job_through(peak):
    """The job's own `run_now` is the per-card flag's twin: explicit wins over the price."""
    job = _job(run_now=True)

    fire, held = sq.hold_jobs([job], _gate(True), now=time.time())

    assert fire == [job]
    assert held == []


# ── the deferral is priced against the provider the job RUNS on ────────────


def test_the_window_is_checked_against_the_route_the_job_will_run_on(monkeypatch):
    """Ask about the FALLBACK's price, never the walled provider's flat-rate plan.

    The walled provider here is `custom` (a flat-rate coding plan) — deferring it would cost
    time and save nothing. The route the fire path installs is the metered fallback, so that
    is the provider whose window must decide.
    """
    seen: dict = {}

    class _Probe:
        is_deferred = True
        resume_at = time.time() + 60
        reason = "peak_hours"

        def covers(self, provider):
            seen["provider"] = provider
            return True

    monkeypatch.setattr(
        "hermes_cli.kanban_cost_window.read_cost_window_state", lambda **kw: _Probe(),
        raising=True,
    )

    sq.cost_window_deferral(_job(), _gate(True), now=time.time())

    assert seen["provider"] == FALLBACK[0]


def test_the_reader_fails_open_when_it_raises(monkeypatch):
    """A broken reader must not park the fleet — the cost of guessing is money, not a stall."""
    def _boom(**kw):
        raise RuntimeError("no clock")

    monkeypatch.setattr(
        "hermes_cli.kanban_cost_window.read_cost_window_state", _boom, raising=True,
    )

    job = _job()
    fire, held = sq.hold_jobs([job], _gate(True), now=time.time())

    assert fire == [job]
    assert held == []


# ── the injected clock is normalised to the shape the reader takes ─────────


def test_a_datetime_now_is_accepted_and_reaches_the_reader_as_an_epoch(monkeypatch):
    """`now` is a plain injection point, so it must not blow up when datetime-shaped."""
    from datetime import datetime, timezone

    seen: dict = {}

    class _Off:
        is_deferred = False
        resume_at = None
        reason = "off_peak"

        def covers(self, provider):
            return False

    def _read(**kw):
        seen.update(kw)
        return _Off()

    monkeypatch.setattr(
        "hermes_cli.kanban_cost_window.read_cost_window_state", _read, raising=True,
    )

    moment = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)
    fire, held = sq.hold_jobs([_job()], _gate(True), now=moment)

    assert fire == [_job()], "off-peak: the job runs"
    assert held == []
    assert isinstance(seen["now"], float)
    assert seen["now"] == moment.timestamp()


def test_as_epoch_passes_through_a_plain_number_and_rejects_junk():
    assert sq._as_epoch(1234.5) == 1234.5
    assert sq._as_epoch(0) == 0.0
    assert sq._as_epoch(None) is None
    assert sq._as_epoch(True) is None
    assert sq._as_epoch("2026-10-07") is None


# ── the Chinese public-holiday carve-out reaches cron too ──────────────────
#
# The provider's rule is Mon-Fri EXCLUDING Chinese public holidays, and the carve-out lives
# INSIDE `read_cost_window_state` (it is answered before the peak-window math, because it can
# only REMOVE a deferral). So cron inherits it by consulting the same reader — but that is
# exactly the kind of parity that rots, so it is pinned here against the REAL reader rather
# than a stubbed state: a cron-specific window check that bypassed the holiday branch would
# pay double on a day the kanban dispatcher knows is cheap.


def _real_reader(monkeypatch, tmp_path, *, holidays, valid_through="2026-12-31"):
    """Point the REAL cost-window reader at a synthetic holiday snapshot.

    Returns the pinned instant (a weekday inside the 01:00-04:00 UTC peak window).
    """
    from hermes_cli import kanban_cost_window as kcw

    cal = tmp_path / "cn-public-holidays.json"
    cal.write_text(json.dumps({
        "version": 1,
        "valid_through": valid_through,
        "years": {"2026": {"holidays": holidays, "workdays": {}}},
    }), encoding="utf-8")
    kcw._holiday_cache.update({"key": None, "data": None})  # no cross-test cache
    block = {
        "enabled": True,
        "peak_windows_utc": ["01:00-04:00", "06:00-10:00"],
        "peak_days": ["mon", "tue", "wed", "thu", "fri"],
        "holiday_calendar": str(cal),
    }
    monkeypatch.setattr(kcw, "_cost_window_config", lambda: block)
    monkeypatch.setattr(kcw, "_bypass_until", lambda now=None: None)
    monkeypatch.setattr(kcw, "_ntp_synchronized", lambda now=None: True)
    # 2026-10-05 is a Monday; 02:00 UTC is inside peak and 10:00 Beijing the same day.
    at = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)
    return at.timestamp()


def test_a_chinese_holiday_releases_the_released_job(monkeypatch, tmp_path):
    """Same treatment as a kanban card: the carve-out removes the deferral for cron too."""
    epoch = _real_reader(monkeypatch, tmp_path, holidays={"2026-10-05": "国庆节(休)"})

    job = _job()
    fire, held = sq.hold_jobs([job], _gate(True), now=epoch)

    assert fire == [job], "a released job must run on a holiday weekday, not wait"
    assert held == []


def test_without_the_carve_out_the_same_job_is_held(monkeypatch, tmp_path):
    """The control: the SAME weekday and instant, with no holiday snapshot, defers.

    This is what makes the test above meaningful — it proves the release is caused by the
    carve-out rather than by the instant simply being off-peak.
    """
    epoch = _real_reader(monkeypatch, tmp_path, holidays={})

    job = _job()
    fire, held = sq.hold_jobs([job], _gate(True), now=epoch)

    assert fire == []
    assert held == [job]


def test_an_expired_holiday_snapshot_falls_back_to_peak(monkeypatch, tmp_path):
    """Fail-safe direction: a stale calendar must defer, never guess a holiday."""
    epoch = _real_reader(
        monkeypatch, tmp_path,
        holidays={"2026-10-05": "国庆节(休)"},
        valid_through="2026-01-01",  # already expired
    )

    job = _job()
    fire, held = sq.hold_jobs([job], _gate(True), now=epoch)

    assert fire == [], "an expired snapshot must not be believed"
    assert held == [job]


# ── the re-arm targets the window end, not the quota reset ─────────────────


def test_the_peak_hold_re_arms_the_job_to_the_windows_end(monkeypatch, peak):
    """Re-arming to the quota reset would waste the very hours the release unlocked."""
    updates: list = []
    monkeypatch.setattr("cron.jobs.update_job", lambda jid, upd: updates.append((jid, upd)))

    job = _job()
    sq.hold_jobs([job], _gate(True, reset_in=6 * 86400), now=time.time())

    assert updates, "the peak-held job must be re-armed"
    jid, upd = updates[0]
    assert jid == "j1"
    armed_at = upd["next_run_at"]
    # The window ends in ~1h; the quota resets in ~6 days. The re-arm must be the former.
    assert "off-peak" in upd["last_error"]
    from datetime import datetime

    hours_out = (datetime.fromisoformat(armed_at) - datetime.now()).total_seconds() / 3600
    assert hours_out < 24, f"re-armed {hours_out:.1f}h out — that is the reset, not the window"
