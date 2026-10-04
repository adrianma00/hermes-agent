"""Holiday carve-out in the cost-window gate: the fail-safe matrix.

The carve-out can only REMOVE a deferral, so every branch here is about one
asymmetry: believing a holiday that is not one would dispatch INTO peak at double
price, while ignoring a real one merely defers work that this gate was already
delaying by design. So the rule every case pins is "any doubt ⇒ peak".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban_cost_window as kcw
from hermes_cli.cost_window import _fmt_holidays, _status_line

# 2026-10-05 is a Monday; 02:00 UTC sits inside the first peak window (01:00-04:00)
# and is 10:00 the same calendar day in Beijing, so the holiday's date is unambiguous.
MONDAY = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)
SATURDAY = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)


def snapshot(tmp_path: Path, *, valid_through: str = "2026-12-31",
             holidays: dict | None = None, workdays: dict | None = None,
             year: str = "2026", raw: str | None = None) -> Path:
    path = tmp_path / "cn-public-holidays.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    path.write_text(json.dumps({
        "version": 1,
        "generated_at": "2026-10-04 19:00:00 SGT",
        "valid_through": valid_through,
        "source": {"repo": "https://github.com/vsme/chinese-days", "npm_version": "1.5.9"},
        "years": {year: {"holidays": holidays or {}, "workdays": workdays or {}}},
    }), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """No NTP shell-out, no bypass board read, no cross-test holiday cache."""
    kcw._holiday_cache.update({"key": None, "data": None})
    monkeypatch.setattr(kcw, "_bypass_until", lambda now=None: None)
    monkeypatch.setattr(kcw, "_ntp_synchronized", lambda now=None: True)
    kcw._clock_unsynced_since = None
    yield
    kcw._holiday_cache.update({"key": None, "data": None})


def configure(monkeypatch, calendar) -> None:
    block = {
        "enabled": True,
        "peak_windows_utc": ["01:00-04:00", "06:00-10:00"],
        "peak_days": ["mon", "tue", "wed", "thu", "fri"],
    }
    if calendar is not None:
        block["holiday_calendar"] = str(calendar)
    monkeypatch.setattr(kcw, "_cost_window_config", lambda: block)


def state(monkeypatch, calendar, at=MONDAY):
    configure(monkeypatch, calendar)
    return kcw.read_cost_window_state(enabled=True, now=at.timestamp())


# ── the default: no snapshot, behaviour unchanged ────────────────────────────
def test_no_calendar_configured_defers_on_a_weekday_and_says_off(monkeypatch):
    st = state(monkeypatch, None)
    assert st.is_deferred and st.reason == "peak_hours"
    assert st.holiday_calendar == ""
    assert st.holiday_usable is False and st.holiday_reason == "off"


def test_calendar_configured_but_absent_file_defers(monkeypatch, tmp_path):
    st = state(monkeypatch, tmp_path / "gone.json")
    assert st.is_deferred and st.reason == "peak_hours"
    assert st.holiday_usable is False and "unreadable" in st.holiday_reason


def test_corrupt_file_defers(monkeypatch, tmp_path):
    bad = snapshot(tmp_path, raw="{not json")
    st = state(monkeypatch, bad)
    assert st.is_deferred and st.holiday_usable is False
    assert "unparseable" in st.holiday_reason


# ── the feature itself ──────────────────────────────────────────────────────
def test_holiday_removes_the_deferral(monkeypatch, tmp_path):
    cal = snapshot(tmp_path, holidays={"2026-10-05": "国庆节(休)"})
    st = state(monkeypatch, cal)
    assert st.is_deferred is False
    assert st.reason == "off_peak_holiday"
    assert st.holiday_today == "国庆节(休)"
    assert st.holiday_usable is True and st.holiday_off_days == 1


def test_a_normal_weekday_still_defers(monkeypatch, tmp_path):
    cal = snapshot(tmp_path, holidays={"2026-10-06": "国庆节(休)"})
    st = state(monkeypatch, cal)
    assert st.is_deferred and st.reason == "peak_hours"
    assert st.holiday_usable is True and st.holiday_today == ""


def test_weekend_holiday_is_off_peak_anyway(monkeypatch, tmp_path):
    cal = snapshot(tmp_path, holidays={"2026-10-03": "国庆节(休)"})
    st = state(monkeypatch, cal, at=SATURDAY)
    assert st.is_deferred is False
    assert st.reason == "off_peak_holiday" and st.holiday_today == "国庆节(休)"


# ── fail-safe: stale / incomplete data must not spend money ──────────────────
def test_expired_snapshot_is_ignored(monkeypatch, tmp_path):
    cal = snapshot(tmp_path, valid_through="2026-09-30",
                   holidays={"2026-10-05": "国庆节(休)"})
    st = state(monkeypatch, cal)
    assert st.is_deferred, "an expired snapshot must defer, not trust last year's calendar"
    assert st.reason == "peak_hours"
    assert st.holiday_usable is False and "expired" in st.holiday_reason


def test_year_not_covered_is_ignored(monkeypatch, tmp_path):
    cal = snapshot(tmp_path, year="2025", holidays={"2025-10-06": "国庆节(休)"})
    st = state(monkeypatch, cal)
    assert st.is_deferred and st.holiday_usable is False
    assert "not covered" in st.holiday_reason


def test_snapshot_without_valid_through_is_ignored(monkeypatch, tmp_path):
    path = tmp_path / "cn-public-holidays.json"
    path.write_text(json.dumps({"version": 1, "years": {"2026": {
        "holidays": {"2026-10-05": "国庆节(休)"}, "workdays": {}}}}), encoding="utf-8")
    st = state(monkeypatch, path)
    assert st.is_deferred and st.holiday_usable is False
    assert "valid_through" in st.holiday_reason


def test_makeup_workdays_never_create_peak(monkeypatch, tmp_path):
    """A make-up WORKING day (调休) is still a weekend day for the provider's rule."""
    cal = snapshot(tmp_path, workdays={"2026-10-05": "国庆节(班)"})
    st = state(monkeypatch, cal)
    assert st.is_deferred, "a make-up workday must not be read as a holiday"
    assert st.holiday_usable is True and st.holiday_today == ""


def test_cache_notices_a_rewritten_snapshot(monkeypatch, tmp_path):
    """The refresher rewrites the file in place; the gate must not serve the old read."""
    cal = snapshot(tmp_path, holidays={"2026-10-06": "国庆节(休)"})
    assert state(monkeypatch, cal).is_deferred is True
    cal.write_text(json.dumps({
        "version": 1, "valid_through": "2026-12-31",
        "years": {"2026": {"holidays": {"2026-10-05": "国庆节(休)", "2026-10-06": "国庆节(休)"},
                           "workdays": {}}}}), encoding="utf-8")
    st = state(monkeypatch, cal)
    assert st.reason == "off_peak_holiday", "cache must key on mtime+size, not path alone"


# ── the operator-facing surface ─────────────────────────────────────────────
def test_status_line_names_the_holiday(monkeypatch, tmp_path):
    cal = snapshot(tmp_path, holidays={"2026-10-05": "国庆节(休)"})
    st = state(monkeypatch, cal)
    line = _status_line(st)
    assert "OFF-PEAK" in line and "国庆节(休)" in line


def test_fmt_holidays_reports_off_active_and_unusable(monkeypatch, tmp_path):
    assert "off" in _fmt_holidays(state(monkeypatch, None))
    active = state(monkeypatch, snapshot(tmp_path, holidays={"2026-10-05": "x"}))
    assert "active" in _fmt_holidays(active) and "2026-12-31" in _fmt_holidays(active)
    expired = state(monkeypatch, snapshot(tmp_path, valid_through="2026-01-01"))
    assert "UNAVAILABLE" in _fmt_holidays(expired)
