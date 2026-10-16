"""The release-all switch, third reader: ``api_server`` turns obey the gate too.

The kanban dispatcher and the cron tick already share one gate card
(``quota-gate:<provider>`` on the fleet board). The ``api_server`` platform — the transport
every inbound peer DM arrives on (``hermes peer dm`` → ``POST /api/sessions/<id>/chat``) —
did not read it at all, so a peer message was the ONE path that got neither the hold nor the
fallback route: the turn ran on the walled provider and the caller was handed the provider's
raw 429. Live: the Mac peer's DM to Em during the 2026-10-10 monthly wall came back as
``HTTP 429 … monthly usage quota``, which made the peer channel one-way for the whole wall.

These tests pin the decision table and the two call sites (``_run_agent`` for the chat doors,
``api_server_runs._execute_run`` for ``/v1/runs``):

  * open gate / disabled gate  → the turn runs untouched (no install-wide behaviour change);
  * closed, HOLD               → the turn does not run and the caller gets ``hold_text`` as
                                 its reply, with no agent ever created;
  * closed, RELEASE-ALL        → the turn runs on the fallback head, for that turn only, and
                                 is recorded with ``source=release_all``;
  * closed, released, no route → held with ``no_fallback_route``, like a released card/job;
  * closed, released, turn pinned to the walled provider → held with ``pinned_to_walled`` (the
                                 pin, not a missing route, is the reason), never re-routed;
  * an exempt provider (request pin, session ``/model`` pin, or the install's own primary)
                               → never paused;
  * a gate card whose wall ENDED (``reset_at`` passed) but which is still on the board
                               → reaped before the read, so its ``release_all`` cannot govern a
                                 later wall's peer DM.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms import api_server_quota_gate as _qg
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms import api_server_runs as _runs
from hermes_cli import kanban_db as _kb
from hermes_cli.quota_state import QuotaGateState
from tools.bot_failure_reasons import RETRY_NONE, result_retry_action

WALLED = "modelark-coding"
FALLBACK = ("deepseek", "deepseek-v4-flash")

def _gate(release_all: bool = False, *, route: bool = True, closed: bool = True,
          walled: str = WALLED, reset_in: float = 6 * 86400) -> QuotaGateState:
    """A closed gate; ``release_all`` is the operator switch under test.
    Phase 2B: constructs QuotaGateState directly (local DB, no board card)."""
    return QuotaGateState(
        is_closed=closed,
        reset_at=time.time() + reset_in if closed else None,
        walled_provider=walled,
        window="monthly",
        fallback_model=FALLBACK[1] if route else None,
        fallback_provider=FALLBACK[0] if route else None,
        release_all=release_all,
    )

# ── the decision table ─────────────────────────────────────────────────────

def test_an_open_or_disabled_gate_never_touches_a_turn():
    """The control that matters most: an install that never enabled the gate is unchanged."""
    for state in (None, QuotaGateState(is_closed=False)):
        decision = _qg.decide_turn(state, primary_provider=WALLED)

        assert decision.action == _qg.RUN
        assert decision.reason == "gate_open"

def test_hold_mode_holds_a_turn_on_the_walled_primary():
    decision = _qg.decide_turn(_gate(False), primary_provider=WALLED)

    assert decision.action == _qg.HOLD
    assert decision.reason == "gate_closed"
    assert decision.walled_provider == WALLED

def test_release_all_routes_the_turn_and_marks_the_audit_source():
    decision = _qg.decide_turn(_gate(True), primary_provider=WALLED)

    assert decision.action == _qg.ROUTE
    assert (decision.provider, decision.model) == FALLBACK
    assert decision.source == _qg.RELEASE_SOURCE == "release_all"

def test_release_all_without_a_route_holds_like_every_other_work_source():
    decision = _qg.decide_turn(_gate(True, route=False), primary_provider=WALLED)

    assert decision.action == _qg.HOLD
    assert decision.reason == "no_fallback_route"

def test_a_request_pinned_to_another_provider_is_exempt():
    decision = _qg.decide_turn(_gate(False), pinned_provider="deepseek", primary_provider=WALLED)

    assert decision.action == _qg.RUN
    assert decision.reason == "exempt"

def test_a_session_model_override_to_another_provider_is_exempt():
    """`/model` is a user pin, not a routing accident — it outlives the wall."""
    decision = _qg.decide_turn(_gate(False), session_provider="deepseek", primary_provider=WALLED)

    assert decision.action == _qg.RUN
    assert decision.reason == "exempt"

def test_an_install_whose_primary_is_not_the_walled_provider_is_exempt():
    decision = _qg.decide_turn(_gate(False), primary_provider="openrouter")

    assert decision.action == _qg.RUN
    assert decision.reason == "exempt"

def test_a_pin_to_the_walled_provider_still_waits_and_is_never_silently_rerouted():
    """Same refusal as a card-level pin in the dispatcher: the caller's choice is not overridden
    — it waits (and is told why) rather than spending the walled quota. The reason differs by
    gate mode: with the switch OFF the wall is the reason; with RELEASE-ALL the PIN is, and
    saying "no fallback route" there would send the caller after a config that is not at fault."""
    decision = _qg.decide_turn(_gate(False), pinned_provider=WALLED, primary_provider=WALLED)

    assert decision.action == _qg.HOLD
    assert decision.reason == "gate_closed"
    assert decision.provider is None and decision.model is None

    released = _qg.decide_turn(_gate(True), pinned_provider=WALLED, primary_provider=WALLED)

    assert released.action == _qg.HOLD
    assert released.reason == "pinned_to_walled"
    assert released.provider is None and released.model is None
    # The route IS configured — the decision has to carry that, or the copy lies.
    assert released.route_available is True

def test_a_session_pin_to_the_walled_provider_waits_the_same_way():
    decision = _qg.decide_turn(_gate(True), session_provider=WALLED, primary_provider=WALLED)

    assert decision.action == _qg.HOLD
    assert decision.reason == "pinned_to_walled"

def test_a_pinned_turn_with_no_route_is_still_reported_as_pinned():
    """Both facts are true here; the pin is the one the caller can act on."""
    decision = _qg.decide_turn(
        _gate(True, route=False), pinned_provider=WALLED, primary_provider=WALLED)

    assert decision.action == _qg.HOLD
    assert decision.reason == "pinned_to_walled"
    assert decision.route_available is False

def test_a_confirmed_runtime_lock_is_left_alone():
    """The locked provider is not visible here, so a locked turn is never re-routed and never
    held: gating it could hold a session already pinned to a healthy provider."""
    decision = _qg.decide_turn(_gate(True), primary_provider=WALLED, confirmed_runtime_lock=True)

    assert decision.action == _qg.RUN
    assert decision.reason == "runtime_locked"

def test_the_gate_is_read_from_the_shared_card(monkeypatch):
    """`decide_for_turn` is the one call the api_server doors make; it must read the gate."""
    monkeypatch.setattr(_qg, "gate_state", lambda: _gate(True))

    decision = _qg.decide_for_turn()

    assert decision.action == _qg.ROUTE

def test_a_raising_gate_reader_does_not_gate(monkeypatch):
    """A broken reader is not a closed gate: an import/config failure must not cost a fleet its
    peer channel (the cron reader's stated stance)."""
    monkeypatch.setattr(_qg, "gate_state", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert _qg.decide_for_turn().action == _qg.RUN

# ── what a held turn hands back ────────────────────────────────────────────

def test_hold_text_names_the_wall_the_reset_and_the_way_out():
    decision = _qg.decide_turn(_gate(False, reset_in=2 * 86400), primary_provider=WALLED)

    text = _qg.hold_text(decision)

    assert text.startswith(_qg.HOLD_PREFIX)
    assert WALLED in text and "monthly" in text
    assert time.strftime("%Y-%m-%d", time.localtime(decision.reset_at)) in text
    assert "hermes quota release-all on" in text
    assert "do not resend before then" in text.lower()

def test_hold_text_explains_a_missing_route():
    decision = _qg.decide_turn(_gate(True, route=False), primary_provider=WALLED)

    text = _qg.hold_text(decision)

    assert "no fallback route" in text
    assert decision.reason in text or "fallback_providers" in text

def test_hold_text_for_a_pinned_turn_blames_the_pin_not_a_missing_route():
    """The defect this pins: RELEASE-ALL + a pin to the walled provider used to tell the caller
    that no fallback route was configured, when the route was configured and the pin was the
    reason the turn did not use it."""
    decision = _qg.decide_turn(_gate(True), pinned_provider=WALLED, primary_provider=WALLED)

    text = _qg.hold_text(decision)

    assert "pin" in text
    assert "no fallback route" not in text
    assert "fallback_providers" not in text
    # The route exists, so the copy can name the way out: drop the pin.
    assert "without the pin" in text

def test_hold_text_for_a_pinned_turn_without_a_route_says_both():
    decision = _qg.decide_turn(
        _gate(True, route=False), pinned_provider=WALLED, primary_provider=WALLED)

    text = _qg.hold_text(decision)

    assert "pin" in text
    assert "No fallback route is configured either" in text

def test_no_hold_variant_claims_the_message_was_stored():
    """Nothing writes the inbound row on the held path (no agent is created), so every variant
    must tell the peer the message is gone and has to be re-sent."""
    decisions = [
        _qg.decide_turn(_gate(False), primary_provider=WALLED),
        _qg.decide_turn(_gate(True, route=False), primary_provider=WALLED),
        _qg.decide_turn(_gate(True), pinned_provider=WALLED, primary_provider=WALLED),
    ]

    for decision in decisions:
        text = _qg.hold_text(decision)

        assert "NOT in the session transcript" in text
        assert "already in this session's transcript" not in text
        assert "re-send" in text.lower() or "Re-send" in text

def test_a_held_result_is_not_a_failure_and_is_never_retried():
    """The shape is the provider-auth one: text, zero usage, no ``failed`` — so the peer client
    delivers the explanation and the transport does not re-run the turn into the same wall."""
    result, usage = _qg.held_result(_qg.decide_turn(_gate(False), primary_provider=WALLED))

    assert result["final_response"].startswith(_qg.HOLD_PREFIX)
    assert not result.get("failed")
    assert result_retry_action(result) == RETRY_NONE
    assert result["quota_hold"]["reason"] == "gate_closed"
    assert usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

def test_a_released_turn_is_recorded_with_the_release_all_source(caplog):
    decision = _qg.decide_turn(_gate(True), primary_provider=WALLED)

    with caplog.at_level("INFO", logger=_qg.__name__):
        _qg.log_release(decision, session_id="s1", door="chat")

    record = caplog.records[-1]
    assert "source=release_all" in record.getMessage()
    assert "deepseek/deepseek-v4-flash" in record.getMessage()

# ── `hermes quota status` still explains the peer path ─────────────────────

def test_quota_status_names_the_peer_dm_path(monkeypatch, capsys):
    class _KB:
        @staticmethod
        def read_quota_gate_state(chain=None, **kwargs):
            return _gate(False)

        @staticmethod
        def quota_gate_fallback_chain():
            return [{"provider": FALLBACK[0], "model": FALLBACK[1]}]

    from hermes_cli import quota as quota_cli

    monkeypatch.setattr(quota_cli, "_kb", lambda: _KB)

    assert quota_cli._cmd_status(argparse.Namespace()) == 0

    out = capsys.readouterr().out
    assert "Mode: HOLD" in out and "peer DMs" in out

# ── the chat doors: `_run_agent` ───────────────────────────────────────────

def _agent_double() -> MagicMock:
    agent = MagicMock()
    agent.session_id = "session"
    agent._last_compaction_in_place = False
    agent.session_prompt_tokens = 1
    agent.session_completion_tokens = 2
    agent.session_total_tokens = 3
    agent.run_conversation.return_value = {"final_response": "served"}
    return agent

async def _chat_turn(tmp_path, monkeypatch, state, *, kwargs=None):
    """One ``_run_agent`` turn with the gate pinned to *state*; returns ``(seen, result, usage)``."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    monkeypatch.setattr(_qg, "gate_state", lambda: state)
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    seen: dict = {}

    def _create_agent(**kw):
        seen.update(kw)
        return _agent_double()

    monkeypatch.setattr(adapter, "_create_agent", _create_agent)
    result, usage = await adapter._run_agent("hi", [], session_id="s1", **(kwargs or {}))
    return seen, result, usage

@pytest.mark.asyncio
async def test_a_disabled_gate_runs_the_turn_untouched(tmp_path, monkeypatch):
    seen, result, _ = await _chat_turn(tmp_path, monkeypatch, None)

    assert seen["requested_provider"] is None and seen["requested_model"] is None
    assert result["final_response"] == "served"

@pytest.mark.asyncio
async def test_a_closed_gate_in_hold_mode_never_creates_an_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    monkeypatch.setattr(_qg, "gate_state", lambda: _gate(False))
    adapter = APIServerAdapter(PlatformConfig(enabled=True))

    def _explode(**_kw):  # pragma: no cover - the assertion IS that this never runs
        raise AssertionError("a held turn must not create an agent")

    monkeypatch.setattr(adapter, "_create_agent", _explode)

    result, usage = await adapter._run_agent("hi", [], session_id="s1")

    assert result["final_response"].startswith(_qg.HOLD_PREFIX)
    assert result["quota_hold"]["reason"] == "gate_closed"
    assert usage["total_tokens"] == 0
    assert adapter._inflight_agent_runs == 0

@pytest.mark.asyncio
async def test_a_released_turn_runs_on_the_fallback_head(tmp_path, monkeypatch, caplog):
    with caplog.at_level("INFO", logger=_qg.__name__):
        seen, result, _ = await _chat_turn(tmp_path, monkeypatch, _gate(True))

    assert (seen["requested_provider"], seen["requested_model"]) == FALLBACK
    assert result["final_response"] == "served"
    assert any("source=release_all" in record.getMessage() for record in caplog.records)

@pytest.mark.asyncio
async def test_a_released_turn_with_no_route_is_held(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    monkeypatch.setattr(_qg, "gate_state", lambda: _gate(True, route=False))
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr(adapter, "_create_agent", MagicMock(side_effect=AssertionError("held")))

    result, _ = await adapter._run_agent("hi", [], session_id="s1")

    assert result["quota_hold"]["reason"] == "no_fallback_route"

# ── the run door: /v1/runs ─────────────────────────────────────────────────

class _RunAdapter:
    """The slice of ``APIServerAdapter`` that ``_execute_run``'s hold path touches."""

    def __init__(self, monkeypatch, state):
        for name in ("_active_run_agents", "_active_run_tasks", "_run_approval_sessions",
                     "_stopping_run_ids", "_shutdown_interrupted_run_ids", "_run_streams"):
            setattr(self, name, {})
        self._owners: dict = {}
        self._statuses: dict = {}
        self.created = 0
        monkeypatch.setattr(_qg, "gate_state", lambda: state)
        monkeypatch.setattr(_runs, "_retire_live_run", lambda s, run_id: None)

    def _profile_scope(self, _profile):
        return nullcontext()

    def _set_run_status(self, run_id, status, **fields):
        self._statuses[run_id] = {"status": status, **fields}

    def _create_agent(self, **_kw):
        self.created += 1
        return _agent_double()

    def _session_model_override_for(self, _key):
        return None

    def _make_run_event_callback(self, run_id, loop):
        return lambda *args, **kwargs: None

#: ``_execute_run`` takes the api_server MODULE (``sys.modules[__name__]``), not the adapter.
class _FakeProviderAuthError(RuntimeError):
    """Distinct from a plain RuntimeError: the real handler must not claim our fixture."""

_FAKE_API_SERVER = SimpleNamespace(
    _redact_api_error_text=lambda value, **kwargs: str(value),
    _ProviderAuthResolutionError=_FakeProviderAuthError)

def _launch(adapter, run_id, **agent_kwargs):
    return SimpleNamespace(
        run_id=run_id, session_id=f"s-{run_id}", gateway_session_key=f"s-{run_id}",
        approval_session_key=run_id, queue=None, request_profile=None,
        # Upstream's _execute_run now branches on the run's admission (its own dataclass
        # defaults the field to None), so this duck-typed stand-in must carry it too or the
        # branch raises AttributeError before the quota gate is ever consulted.
        admission=None,
        agent_kwargs={"requested_provider": None, "requested_model": None, **agent_kwargs},
        put_event=lambda _e: None, owner=adapter)

@pytest.mark.asyncio
async def test_a_held_run_fails_with_the_hold_text_and_never_builds_an_agent(monkeypatch):
    adapter = _RunAdapter(monkeypatch, _gate(False))

    await _runs._execute_run(adapter, _launch(adapter, "run1"), _api_server=_FAKE_API_SERVER)

    status = adapter._statuses["run1"]
    assert status["status"] == "failed"
    assert status["reason"] == "quota_hold"
    assert status["error"].startswith(_qg.HOLD_PREFIX)
    assert adapter.created == 0

@pytest.mark.asyncio
async def test_a_run_pinned_to_the_walled_provider_is_held_not_429d(monkeypatch):
    """The run door holds a pinned run in both gate modes, and under RELEASE-ALL it says the pin
    is why — the same reason the chat door reports."""
    for state, needle in ((_gate(False), "HOLD mode"), (_gate(True), "pin")):
        adapter = _RunAdapter(monkeypatch, state)

        await _runs._execute_run(
            adapter, _launch(adapter, "run2", requested_provider=WALLED), _api_server=_FAKE_API_SERVER)

        status = adapter._statuses["run2"]
        assert status["reason"] == "quota_hold"
        assert needle in status["error"]
        assert adapter.created == 0

@pytest.mark.asyncio
async def test_an_exempt_run_is_not_held(monkeypatch):
    """A run pinned to a healthy provider keeps running while the wall is up."""
    adapter = _RunAdapter(monkeypatch, _gate(False))
    reached: list = []

    def _past_the_gate(loop, fn):
        reached.append(True)
        raise RuntimeError("the fake adapter has no agent lifecycle")

    monkeypatch.setattr(_runs, "_submit_api_worker", _past_the_gate)

    await _runs._execute_run(
        adapter, _launch(adapter, "run3", requested_provider="deepseek"), _api_server=_FAKE_API_SERVER)

    assert reached == [True]
    assert adapter._statuses["run3"].get("reason") != "quota_hold"

# ── the FIFTH behaviour case: a wall that ENDED cannot govern a peer DM ────
#
# Phase 2B: local state DB auto-filters expired walls. A wall that ends on its own is simply
# not returned by the reader (reset_at > now). No reap needed, no inheritance problem.
#
# ── harness ────────────────────────────────────────────────────────────────

@pytest.fixture
def gate_board(tmp_path, monkeypatch):
    """An isolated home whose gate the real reader resolves.

    Mirrors ``tests/hermes_cli/test_kanban_quota_release_all.py``'s fixture, so the peer reader is
    exercised against the REAL local state DB rather than a stubbed ``QuotaGateState``.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    board = "gate-board"
    monkeypatch.setattr(_kb, "_quota_gate_config", lambda: (True, board))
    # The fallback route the gate would send a released turn to.
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"fallback_providers": [{"provider": FALLBACK[0], "model": FALLBACK[1]}]})
    # A turn on the install's own primary IS a turn on the walled provider, unless pinned.
    monkeypatch.setattr(_qg, "configured_primary_provider", lambda: WALLED)

    return board

def _raw_wall_count() -> int:
    """Rows physically on file, expired or not (``list_walls`` hides expired)."""
    from hermes_cli.quota_state import get_db_path
    conn = sqlite3.connect(str(get_db_path()))
    try:
        return conn.execute("SELECT COUNT(*) FROM wall_state").fetchone()[0]
    finally:
        conn.close()

def _arm_the_wall(gate_board=None, *, release_all=None, reset_in=6 * 86400):
    """A future-reset wall via local state DB, optionally with the switch thrown."""
    from hermes_cli.quota_state import write_wall, set_release_all
    write_wall(WALLED, window="monthly", reset_at=int(time.time()) + reset_in, source="test")
    if release_all is not None:
        set_release_all(WALLED, release_all)

def _expire_the_wall(gate_board=None):
    """Rewrite the wall's reset_at into the past — via local state DB."""
    from hermes_cli.quota_state import write_wall
    write_wall(WALLED, window="monthly", reset_at=int(time.time()) - 60, source="test-expired")

def _gate_card_count(gate_board=None) -> int:
    from hermes_cli.quota_state import list_walls
    return len(list_walls())

def test_a_wall_that_ended_by_clock_cannot_govern_a_peer_dm(gate_board):
    """The fifth behaviour case, end to end.

    Wall 1: the operator throws the switch. It ends on its own. A genuinely NEW wall then
    arrives; the peer DM against it must be HELD — not routed by a switch thrown against the
    wall that already ended.
    """
    _arm_the_wall(gate_board, release_all=True)
    _expire_the_wall(gate_board)
    # The premise, stated plainly: the reader hides the ended wall, but its row lingers.
    assert _gate_card_count(gate_board) == 0, "an ended wall is not returned by the reader"
    assert _raw_wall_count() == 1, "its row stays on file until a write or a sweep clears it"

    # One peer turn is enough to notice — an ended wall reads as an OPEN gate.
    assert _qg.gate_state() is None, "an ended wall reads as an OPEN gate"

    # A genuinely new wall arrives (a fresh 429); it must start in HOLD, inheriting nothing.
    _arm_the_wall(gate_board)

    decision = _qg.decide_for_turn()

    assert decision.action == _qg.HOLD
    assert decision.reason == "gate_closed"
    assert decision.source == ""

