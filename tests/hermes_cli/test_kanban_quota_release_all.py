"""The release-all switch: one operator lever that stops holding kanban cards during a wall.

The provider-quota gate holds ready cards while the primary's subscription window is
closed. That is the right default when the reset is minutes-to-hours away, and the wrong
answer when it is days away — the board then just sits there re-pausing the same cards
every tick (observed live 2026-10-07: 2 cards, 2,783 ``quota_paused`` rows, reset 5.9 days
out, while interactive turns were failing over happily).

So a wall carries an operator switch, column ``release_all`` in the local state DB:

  * HOLD (default, "gate ON for kanban") — ready cards wait for the reset; the manual
    per-card ``quota_override`` is the only way through.
  * RELEASE-ALL ("gate OFF for kanban") — nothing is held; every spawn the gate would
    have paused is routed to the fallback head, in memory only, exactly like
    ``quota_override``.

It is POLICY stored per walled provider in the local state DB (``quota_state.db``), and
that placement is the feature: it governs every work source that obeys the gate, and it
dies with the wall, so the next wall starts in HOLD rather than inheriting a decision made
against a different reset distance. These tests pin the things that would silently break
that:

  1. a 429 refresh must not revert the switch — the trigger rewrites the wall,
  2. the toggle must edit the wall the DISPATCHER reads,
  3. the dispatcher must actually route a released card, and record that it did,
  4. a wall that ended on its own must not carry its switch into the next one.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.quota_state import (
    delete_wall,
    list_walls,
    read_state,
    set_release_all,
    sweep_expired,
    write_wall,
)

FALLBACK = {"provider": "deepseek", "model": "deepseek-v4-flash"}
WALLED = "custom"


# ── harness ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Every test gets its own throwaway home: the quota-state DB and the kanban
    DB both resolve under it, so nothing ever touches the operator's real state.

    Phase 2B: the gate lives in the local state DB (``quota_state.db``); a shared
    board card no longer exists, so the fixture only has to isolate storage.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


@pytest.fixture
def gate_board(monkeypatch):
    """The gate is enabled and resolved from the isolated local store."""
    board = "gate-board"
    monkeypatch.setattr(kb, "_quota_gate_config", lambda: (True, board))
    return board


def _config_with_route(monkeypatch, *entries):
    """Point the shared fallback-chain resolver at ``entries`` (or at nothing)."""
    cfg = {"fallback_providers": list(entries)}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)


def _close_gate(monkeypatch, board=None, *, release_all=None, reset_in=6 * 86400):
    """A future-reset wall via local state DB, optionally with the switch thrown."""
    from hermes_cli.quota_state import write_wall, set_release_all
    reset_at = int(time.time()) + reset_in
    write_wall(WALLED, window="monthly", reset_at=reset_at, source="test")
    if release_all is not None:
        set_release_all(WALLED, release_all)
    return reset_at


def _card_body(board=None):
    from hermes_cli.quota_state import list_walls
    walls = list_walls()
    if not walls:
        return None
    return {"provider": walls[0]["provider"], "window": walls[0]["window"],
            "reset_at": walls[0]["reset_at"], "release_all": bool(walls[0]["release_all"])}


def _events(conn, task_id, kind):
    return [
        json.loads(row["payload"]) if row["payload"] else {}
        for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, kind),
        )
    ]


# ── 1. the switch rides the wall, and a refresh cannot revert it ────────────


def test_reader_surfaces_the_switch_from_the_card(gate_board):
    _close_gate(None, gate_board, release_all=True)

    state = read_state()

    assert state.is_closed and state.release_all is True


def test_a_card_without_the_key_reads_as_hold(gate_board):
    """The default must be HOLD, not "absent means release"."""
    _close_gate(None, gate_board)

    state = read_state()

    assert state.is_closed and state.release_all is False
    body = _card_body(gate_board)
    assert body is not None and body["release_all"] is False


def test_a_429_refresh_preserves_the_switch(gate_board):
    """The trigger rebuilds the body from scratch; the switch must survive it.

    Without the carry, the operator turns release-all on, the next 429 refresh (or a
    ``hermes quota close``) rewrites the wall, and the fleet silently starts holding
    again with nothing in the logs to explain it.
    """
    _close_gate(None, gate_board, release_all=True)
    from agent import quota_gate_trigger as trigger

    later = time.time() + 12 * 86400
    assert trigger.maybe_close_gate(provider=WALLED, reset_at=later, reason="billing") is True

    body = _card_body(gate_board)
    assert body is not None
    assert body["release_all"] is True        # carried through the refresh
    assert body["reset_at"] == pytest.approx(later)  # ...while the incident advanced


def test_the_switch_survives_the_other_writers_too(gate_board):
    """``hermes quota close`` and the probe's re-close go through the same carry."""
    _close_gate(None, gate_board, release_all=True)

    from hermes_cli.quota_state import write_wall
    write_wall(WALLED, window="5h", reset_at=int(time.time()) + 3600, source="test")

    assert read_state().release_all is True


def test_a_fresh_wall_starts_in_hold():
    """Per-incident semantics: the switch dies with the card, so a new wall holds."""
    _close_gate(None, gate_board, release_all=True)
    assert delete_wall(WALLED) is True

    _close_gate(None, gate_board)  # a new wall, new card

    assert read_state().release_all is False


# ── 1b. the switch must die with the wall even when the wall ENDS ON ITS OWN ──
#
# Phase 2B: local state DB auto-filters expired walls. A wall that ends on its own is simply
# not returned by the reader (reset_at > now). The sweep removes the expired row.


def _expire_the_wall(gate_board=None, *, seconds_ago: float = 60):
    """Rewrite the wall's reset_at into the past via local state DB."""
    from hermes_cli.quota_state import write_wall
    write_wall(WALLED, window="monthly", reset_at=int(time.time() - seconds_ago), source="test-expired")


def _gate_card_count(gate_board=None) -> int:
    from hermes_cli.quota_state import list_walls
    return len(list_walls())


def _raw_wall_count() -> int:
    """Rows physically on file, expired or not (``list_walls`` hides expired)."""
    from hermes_cli.quota_state import get_db_path
    conn = sqlite3.connect(str(get_db_path()))
    try:
        return conn.execute("SELECT COUNT(*) FROM wall_state").fetchone()[0]
    finally:
        conn.close()


def test_an_expired_wall_reads_as_open_but_leaves_its_card_behind():
    """The premise of the hazard: opening by clock does not clean up the row."""
    _close_gate(None, None, release_all=True)
    _expire_the_wall(None)

    assert read_state().is_closed is False
    # The reader filters it, but the row is still on file until a write or a sweep
    # clears it — that lingering row is what a wrong writer could re-arm.
    assert _raw_wall_count() == 1


def test_the_sweep_reaps_an_expired_wall():
    _close_gate(None, None, release_all=True)
    _expire_the_wall(None)

    from hermes_cli.quota_state import sweep_expired
    assert sweep_expired() == 1
    assert _raw_wall_count() == 0
    # A second sweep is a no-op, not an error: it runs on every tick.
    assert sweep_expired() == 0
    assert _gate_card_count() == 0


def test_the_sweep_never_touches_a_live_wall():
    """Sweep does not remove a live (non-expired) wall."""
    _close_gate(None, None, release_all=True)  # reset 6 days out

    from hermes_cli.quota_state import sweep_expired
    assert sweep_expired() == 0
    assert _gate_card_count() == 1
    assert read_state().is_closed is True


def test_a_wall_that_ended_by_clock_does_not_carry_the_switch_into_the_next_one(gate_board):
    """The property the consumer cares about, end to end.

    Wall 1: the operator throws the switch. It ends on its own (the clock passes). The next
    wall must start in HOLD — not inherit a decision made against a different reset.
    """
    _close_gate(None, gate_board, release_all=True)
    _expire_the_wall(gate_board)

    # Whatever the fleet's tick does on noticing the wall is over.
    sweep_expired()

    # A genuinely new wall arrives (a fresh 429).
    _close_gate(None, gate_board)

    state = read_state()
    assert state.is_closed is True
    assert state.release_all is False, "the previous wall's switch was inherited"


def test_a_new_wall_does_not_inherit_through_an_unswept_expired_row(gate_board):
    """The inheritance guard is in the WRITER, not the sweep.

    Phase 2B: the reader filters expired walls but nothing sweeps them on a tick, so the
    expired row is still on file when the next wall for the same provider is written. The
    writer must treat that row as a wall that ENDED and start the new one in HOLD. Without
    this the operator's switch from a 6-day wall would silently govern a later 5-hour one.
    """
    _close_gate(None, gate_board, release_all=True)
    _expire_the_wall(gate_board)
    # No sweep: the expired row is deliberately left behind.
    assert _raw_wall_count() == 1

    _close_gate(None, gate_board)  # the next wall overwrites the same provider

    assert read_state().release_all is False, (
        "an expired wall's switch was inherited by the next one"
    )


def test_toggle_round_trips_on_the_card(gate_board):
    _close_gate(None, gate_board)

    from hermes_cli.quota_state import set_release_all
    assert set_release_all(WALLED, True) is True
    assert read_state().release_all is True
    body = _card_body(gate_board)
    assert body is not None and body["release_all"] is True

    assert set_release_all(WALLED, False) is True
    assert read_state().release_all is False


def test_toggle_with_no_wall_is_not_a_silent_success(gate_board):
    """There is nothing to switch while the gate is open — fail, do not report success."""
    from hermes_cli.quota_state import set_release_all
    assert set_release_all(WALLED, True) is False
    assert read_state().release_all is False
    assert read_state().is_closed is False


def test_toggle_preserves_the_rest_of_the_card(gate_board):
    """It patches one key; it must not rewrite the incident state it found."""
    reset_at = _close_gate(None, gate_board)

    from hermes_cli.quota_state import set_release_all
    set_release_all(WALLED, True)

    state = read_state()
    assert state.release_all is True
    assert state.reset_at == float(reset_at)
    assert state.walled_provider == WALLED
    assert state.window == "monthly"


# ── 2. the CLI reports the mode the dispatcher will honour ──────────────────


def test_cli_release_all_on_names_the_route_and_the_gate_state(gate_board, monkeypatch, capsys):
    _close_gate(None, gate_board)
    _config_with_route(monkeypatch, FALLBACK)
    from hermes_cli import quota as quota_cli

    rc = quota_cli.quota_command(argparse.Namespace(action="release-all", state="on"))

    out = capsys.readouterr().out
    assert rc == 0
    # The operator's own vocabulary: "on" here means the gate is OFF.
    assert "Gate mode: OFF" in out
    assert "deepseek:deepseek-v4-flash" in out
    assert read_state().release_all is True


def test_cli_release_all_off_returns_to_hold(gate_board, monkeypatch, capsys):
    _close_gate(None, gate_board, release_all=True)
    from hermes_cli import quota as quota_cli

    rc = quota_cli.quota_command(argparse.Namespace(action="release-all", state="off"))

    assert rc == 0
    assert "Gate mode: ON" in capsys.readouterr().out
    assert read_state().release_all is False


def test_cli_release_all_on_without_a_route_warns_loudly(gate_board, monkeypatch, capsys):
    """Releasing with nowhere to route would just hold cards again — say it up front."""
    _close_gate(None, gate_board)
    _config_with_route(monkeypatch)
    from hermes_cli import quota as quota_cli

    quota_cli.quota_command(argparse.Namespace(action="release-all", state="on"))

    assert "no fallback route is configured" in capsys.readouterr().out


def test_cli_release_all_with_no_wall_fails_and_explains(gate_board, capsys):
    from hermes_cli import quota as quota_cli

    rc = quota_cli.quota_command(argparse.Namespace(action="release-all", state="on"))

    err = capsys.readouterr().err
    assert rc == 1
    assert "gate is OPEN" in err


@pytest.mark.parametrize("release_all,expected", [(True, "Mode: RELEASE-ALL"), (False, "Mode: HOLD")])
def test_status_names_the_mode(gate_board, monkeypatch, capsys, release_all, expected):
    """`quota status` must say which mode is in force, or it cannot explain a board
    with no `quota_paused` rows while the gate is closed."""
    _close_gate(None, gate_board, release_all=release_all)
    _config_with_route(monkeypatch, FALLBACK)
    from hermes_cli import quota as quota_cli

    assert quota_cli._cmd_status(argparse.Namespace()) == 0

    out = capsys.readouterr().out
    assert expected in out
    assert "Override route: deepseek:deepseek-v4-flash" in out


# ── 3. the dispatcher: released cards spawn on the fallback head ────────────


def _dispatch(board, monkeypatch, *, walled_provider=WALLED, spawns=None, chain=True):
    """One tick against a closed gate; returns ``(result, spawns, conn)``."""
    _config_with_route(monkeypatch, FALLBACK) if chain else _config_with_route(monkeypatch)
    seen = spawns if spawns is not None else []

    def fake_spawn(task, workspace, board=None):
        seen.append(task)
        return 42

    conn = kbc.connect()
    try:
        result = kbd.dispatch_once(conn, spawn_fn=fake_spawn, board=None)
    finally:
        conn.close()
    return result, seen


def test_release_all_spawns_the_held_card_routed_to_the_fallback_head(
    gate_board, all_assignees_spawnable, monkeypatch,
):
    _close_gate(monkeypatch, gate_board, release_all=True)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="held-card", assignee="alice")

    result, spawned = _dispatch(gate_board, monkeypatch)

    assert [t.id for t in spawned] == [task_id]
    assert task_id in [row[0] for row in result.spawned]
    assert result.quota_paused == []
    # The route is computed at spawn time and never stored on the card.
    assert spawned[0].model_override == FALLBACK["model"]
    assert spawned[0].provider_override == FALLBACK["provider"]
    with kbc.connect() as conn:
        assert conn.execute(
            "SELECT model_override, provider_override FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[:] == (None, None)


def test_release_all_records_why_the_card_was_released(gate_board, all_assignees_spawnable, monkeypatch):
    """A closed gate with no `quota_paused` rows reads as a dead dispatcher otherwise."""
    _close_gate(monkeypatch, gate_board, release_all=True)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="held-card", assignee="alice")

    _dispatch(gate_board, monkeypatch)

    with kbc.connect() as conn:
        released = _events(conn, task_id, "quota_released")
    assert len(released) == 1
    assert released[0]["source"] == "release_all"
    assert released[0]["model"] == FALLBACK["model"]
    assert released[0]["provider"] == FALLBACK["provider"]


def test_hold_mode_still_pauses_the_same_card(gate_board, all_assignees_spawnable, monkeypatch):
    """The control: with the switch OFF the card waits, exactly as before the feature."""
    _close_gate(monkeypatch, gate_board, release_all=False)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="held-card", assignee="alice")

    result, spawned = _dispatch(gate_board, monkeypatch)

    assert spawned == []
    assert result.quota_paused == [task_id]
    with kbc.connect() as conn:
        paused = _events(conn, task_id, "quota_paused")
        assert _events(conn, task_id, "quota_released") == []
    assert paused and paused[0]["reason"] == "gate_closed"
    assert paused[0]["release_all"] is False
    assert paused[0]["override"] is False


def test_release_all_never_clobbers_a_deliberate_card_pin(gate_board, all_assignees_spawnable, monkeypatch):
    """A card pinned to the WALLED provider is not exempt, so release-all decides its fate.

    Its own pin still wins over the fleet route — the pin is the card's decision, and the
    worker's in-run failover handles the wall.
    """
    _close_gate(monkeypatch, gate_board, release_all=True)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="pinned-card", assignee="alice")
        conn.execute("UPDATE tasks SET provider_override = ? WHERE id = ?", (WALLED, task_id))
        conn.commit()

    _dispatch(gate_board, monkeypatch)

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT provider_override, model_override FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert row["provider_override"] == WALLED   # untouched by the release route
    assert row["model_override"] is None


def test_release_all_with_no_route_still_holds(gate_board, all_assignees_spawnable, monkeypatch):
    """Authorised but nowhere to go: the card waits, and says why."""
    _close_gate(monkeypatch, gate_board, release_all=True)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="held-card", assignee="alice")

    result, spawned = _dispatch(gate_board, monkeypatch, chain=False)

    assert spawned == []
    assert result.quota_paused == [task_id]
    with kbc.connect() as conn:
        paused = _events(conn, task_id, "quota_paused")
    assert paused and paused[0]["reason"] == "no_fallback_route"
    assert paused[0]["release_all"] is True


def test_a_card_pinned_to_another_provider_still_spawns_on_its_own_pin(
    gate_board, all_assignees_spawnable, monkeypatch,
):
    """Exemption predates this feature and must be unaffected by it."""
    _close_gate(monkeypatch, gate_board, release_all=False)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="exempt-card", assignee="alice")
        conn.execute("UPDATE tasks SET provider_override = ? WHERE id = ?", ("deepseek", task_id))
        conn.commit()

    result, spawned = _dispatch(gate_board, monkeypatch)

    assert [t.id for t in spawned] == [task_id]
    assert spawned[0].provider_override == "deepseek"
    assert result.quota_paused == []


# ── 4. a released card still observes the cost window ──────────────────────
#
# RELEASE-ALL routes each spawn to the FALLBACK head, which is a metered provider with
# peak and off-peak hours. Releasing a card authorises spending the fallback; it must not
# license the peak rate. The dispatcher already defers a card to the fallback's discount
# window (see ``kanban_cost_window``), and these pin that the release does not bypass it —
# the property is easy to lose because the two gates are separate code paths.


def _peak_window(monkeypatch, *, deferred: bool = True, resume_in: float = 3600.0):
    """Make the cost window report peak (or off-peak) on the metered fallback."""
    from hermes_cli.kanban_cost_window import CostWindowState

    state = CostWindowState(
        is_deferred=deferred,
        resume_at=time.time() + resume_in,
        providers=("deepseek",),  # the fallback, from FALLBACK above
        reason="peak_hours",
    )
    monkeypatch.setattr(
        "hermes_cli.kanban_cost_window.read_cost_window_state", lambda **kw: state,
    )
    return state


def test_a_released_card_is_still_deferred_out_of_the_fallbacks_peak_hours(
    gate_board, all_assignees_spawnable, monkeypatch,
):
    """The core property: release passes the QUOTA gate, not the PRICE gate."""
    _close_gate(monkeypatch, gate_board, release_all=True)
    _peak_window(monkeypatch, deferred=True)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="held-card", assignee="alice")

    result, spawned = _dispatch(gate_board, monkeypatch)

    assert spawned == [], "a released card must not spawn at peak price"
    assert result.quota_paused == []
    assert result.cost_deferred == [task_id]

    with kbc.connect() as conn:
        deferred = _events(conn, task_id, "cost_deferred")
    assert len(deferred) == 1
    # Priced against the provider the release ROUTES to, not the walled primary.
    assert deferred[0]["provider"] == FALLBACK["provider"]


def test_a_released_card_spawns_once_the_discount_window_opens(
    gate_board, all_assignees_spawnable, monkeypatch,
):
    """Off-peak: the release does what it promises — the card runs on the fallback."""
    _close_gate(monkeypatch, gate_board, release_all=True)
    _peak_window(monkeypatch, deferred=False)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="held-card", assignee="alice")

    result, spawned = _dispatch(gate_board, monkeypatch)

    assert [t.id for t in spawned] == [task_id]
    assert spawned[0].provider_override == FALLBACK["provider"]
    assert result.cost_deferred == []


def test_hold_mode_never_consults_the_cost_window(
    gate_board, all_assignees_spawnable, monkeypatch,
):
    """Control: with the switch OFF the card waits for the RESET, and the price is moot.

    A card the gate never released is not routed to the metered fallback, so reading the
    cost window would be both pointless and misleading — the hold it produces is the
    quota hold.
    """
    _close_gate(monkeypatch, gate_board, release_all=False)
    _peak_window(monkeypatch, deferred=True)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="held-card", assignee="alice")

    result, spawned = _dispatch(gate_board, monkeypatch)

    assert spawned == []
    assert result.quota_paused == [task_id]
    assert result.cost_deferred == []


def test_run_now_forces_a_released_card_through_peak(
    gate_board, all_assignees_spawnable, monkeypatch,
):
    """The per-card escape hatch survives the release: explicit intent wins over price."""
    _close_gate(monkeypatch, gate_board, release_all=True)
    _peak_window(monkeypatch, deferred=True)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="urgent-card", assignee="alice")
        conn.execute("UPDATE tasks SET run_now = 1 WHERE id = ?", (task_id,))
        conn.commit()

    result, spawned = _dispatch(gate_board, monkeypatch)

    assert [t.id for t in spawned] == [task_id]
    assert spawned[0].provider_override == FALLBACK["provider"]
    assert result.cost_deferred == []
