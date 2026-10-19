"""The release-all switch, second reader: cron jobs follow the SAME card flag.

The kanban dispatcher and the cron tick already share one gate card (`quota-gate:<provider>`
on the fleet board). This pins that they also share the operator's `release_all` switch on
that card: throwing it releases due model-calling cron jobs exactly as it releases ready
kanban cards, and both keep the same two refusals —

  * a job pinned to a DIFFERENT provider is already exempt (unchanged), and
  * with no fallback route configured there is nowhere to send the work, so a released job
    is held like any other rather than burned against the wall.

Two properties worth stating because they are easy to get wrong:

  * the switch must never claim to have routed a job that makes NO model call — a
    `no_agent` script job spends none of the walled quota, so routing it is meaningless and
    logging that it was routed is a lie; and
  * the route is per RUN and never persisted, so it is computed from the gate object the
    tick already read (one read of the card, not two that can disagree).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from cron import scheduler as sched
from cron import scheduler_quota as sq
from hermes_cli.kanban_db import QuotaGateState

WALLED = "custom"
FALLBACK = ("deepseek", "deepseek-v4-flash")


def _gate(release_all: bool = False, *, route: bool = True, reset_in: float = 6 * 86400):
    """A closed gate; ``release_all`` is the operator switch under test."""
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
    }
    job.update(over)
    return job


# ── the switch is the same field, read the same way ────────────────────────


def test_release_all_reads_as_hold_for_unknown_gate_shapes():
    """Fail towards HOLD: an old gate object or a test double must not release work."""
    assert sq.release_all(None) is False
    assert sq.release_all(object()) is False
    assert sq.release_all(SimpleNamespace()) is False
    assert sq.release_all(SimpleNamespace(release_all=True)) is True


def test_override_route_authorises_a_job_with_no_per_job_flag():
    """The switch replaces the per-job flag as the authorisation, same route shape."""
    job = _job()
    assert sq.override_route(job, _gate(False)) is None
    assert sq.override_route(job, _gate(True)) == FALLBACK


def test_a_per_job_override_still_works_and_is_not_the_switch():
    job = _job(quota_override=True)
    assert sq.override_route(job, _gate(False)) == FALLBACK


def test_release_all_without_a_route_authorises_nothing():
    """Nowhere to route = no authorisation; the caller holds the job like any other."""
    assert sq.override_route(_job(), _gate(True, route=False)) is None


# ── the tick: released jobs fire instead of being re-armed ─────────────────


def test_hold_mode_re_arms_a_due_job():
    """The control: without the switch the job is held, as before the feature."""
    job = _job()

    fire, held = sq.hold_jobs([job], _gate(False))

    assert fire == []
    assert held == [job]


def test_release_all_fires_the_same_due_job():
    job = _job()

    fire, held = sq.hold_jobs([job], _gate(True))

    assert fire == [job]
    assert held == []


def test_release_all_leaves_a_script_job_alone():
    """A `no_agent` job makes no model call: it fired before and it fires now."""
    job = _job(no_agent=True)

    fire_hold, held_hold = sq.hold_jobs([job], _gate(False))
    fire_release, held_release = sq.hold_jobs([job], _gate(True))

    assert fire_hold == [job] and held_hold == []
    assert fire_release == [job] and held_release == []


def test_release_all_does_not_touch_an_exempt_job():
    """A job pinned to a healthy provider is exempt on its own terms, in both modes."""
    job = _job(provider="deepseek")

    for gate in (_gate(False), _gate(True)):
        fire, held = sq.hold_jobs([job], gate)
        assert fire == [job] and held == []


def test_release_all_with_no_route_still_holds_the_job():
    job = _job()

    fire, held = sq.hold_jobs([job], _gate(True, route=False))

    assert fire == []
    assert held == [job]


# ── fire time: the released job actually runs on the fallback ──────────────


def _process(monkeypatch, job: dict, gate) -> "tuple[dict, bool]":
    """Run ``_process_due_job`` against faithful doubles.

    Returns ``(the job it would hand to run_one_job, its return value)``.  The claim double
    returns the PERSISTED record for the job — every field, not a bare ``{id}`` — because
    that is what ``claim_job_for_fire(return_job=True)`` really returns and what the route
    logic reads a second time.
    """
    seen: dict = {}

    def _claim(job_id, **kwargs):
        return dict(job, id=job_id)

    def _run(claimed_job, **kwargs):
        seen.update(claimed_job)
        return True

    monkeypatch.setattr(sched, "claim_job_for_fire", _claim)
    monkeypatch.setattr(sched, "run_one_job", _run)
    monkeypatch.setattr(sched, "finish_execution", lambda *a, **kw: None)
    return seen, sched._process_due_job(job, None, None, False, gate=gate)


def test_process_due_job_routes_a_released_job(monkeypatch):
    captured_job, ok = _process(monkeypatch, _job(), _gate(True))

    assert ok is True
    assert captured_job["provider"] == FALLBACK[0]
    assert captured_job["model"] == FALLBACK[1]


def test_process_due_job_leaves_a_held_job_on_the_primary(monkeypatch):
    captured_job, _ = _process(monkeypatch, _job(), _gate(False))

    assert "provider" not in captured_job
    assert "model" not in captured_job


def test_process_due_job_keeps_the_per_job_override_path(monkeypatch):
    """The legacy authorisation is untouched by the switch being off."""
    captured_job, _ = _process(monkeypatch, _job(quota_override=True), _gate(False))

    assert captured_job["provider"] == FALLBACK[0]
    assert captured_job["model"] == FALLBACK[1]


def test_process_due_job_does_not_route_a_script_job(monkeypatch):
    """Routing a job that never calls a model would be a lie in the log, not just noise."""
    captured_job, _ = _process(monkeypatch, _job(no_agent=True), _gate(True))

    assert "provider" not in captured_job
    assert "model" not in captured_job


def test_process_due_job_resolves_the_gate_when_none_is_threaded(monkeypatch):
    """Callers that do not pass the tick's gate must still route a flagged job."""
    monkeypatch.setattr(sq, "gate_state", lambda: _gate(True))

    seen: dict = {}
    monkeypatch.setattr(sched, "claim_job_for_fire", lambda job_id, **kw: _job(quota_override=True))
    monkeypatch.setattr(sched, "run_one_job", lambda job, **kw: seen.update(job) or True)
    sched._process_due_job(_job(quota_override=True), None, None, False)

    assert seen["provider"] == FALLBACK[0]


def test_process_due_job_is_a_no_op_when_the_gate_is_open(monkeypatch):
    """Open gate (``gate_state()`` -> None): nothing to route, run on the primary."""
    monkeypatch.setattr(sq, "gate_state", lambda: None)
    captured_job, _ = _process(monkeypatch, _job(quota_override=True), None)

    assert "provider" not in captured_job
