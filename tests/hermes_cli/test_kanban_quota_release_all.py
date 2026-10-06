"""The release-all switch: one operator lever that stops holding kanban cards during a wall.

The provider-quota gate holds ready cards while the primary's subscription window is
closed. That is the right default when the reset is minutes-to-hours away, and the wrong
answer when it is days away — the board then just sits there re-pausing the same cards
every tick (observed live 2026-10-07: 2 cards, 2,783 ``quota_paused`` rows, reset 5.9 days
out, while interactive turns were failing over happily).

So the gate card carries an operator switch, body key ``release_all``:

  * HOLD (default, "gate ON for kanban") — ready cards wait for the reset; the manual
    per-card ``quota_override`` is the only way through.
  * RELEASE-ALL ("gate OFF for kanban") — nothing is held; every spawn the gate would
    have paused is routed to the fallback head, in memory only, exactly like
    ``quota_override``.

It is POLICY stored on the INCIDENT card, and that placement is the feature: it governs
every install reading the shared board at once, and it dies with the wall, so the next
wall starts in HOLD rather than inheriting a decision made against a different reset
distance. These tests pin the three things that would silently break that:

  1. a 429 refresh must not revert the switch — the trigger rebuilds the card body,
  2. the toggle must edit the card the DISPATCHER reads (newest gate card),
  3. the dispatcher must actually route a released card, and record that it did.
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

FALLBACK = {"provider": "deepseek", "model": "deepseek-v4-flash"}
WALLED = "custom"


# ── harness ─────────────────────────────────────────────────────────────────


@pytest.fixture
def gate_board(tmp_path, monkeypatch):
    """An isolated home whose gate card is the one the reader resolves."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    board = "gate-board"
    kb.create_board(board)
    monkeypatch.setattr(kb, "_quota_gate_config", lambda: (True, board))
    return board


def _config_with_route(monkeypatch, *entries):
    """Point the shared fallback-chain resolver at ``entries`` (or at nothing)."""
    cfg = {"fallback_providers": list(entries)}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)


def _close_gate(monkeypatch, board, *, release_all=None, reset_in=6 * 86400):
    """A future-reset gate card on ``board``, optionally with the switch thrown."""
    reset_at = int(time.time()) + reset_in
    kb.write_quota_gate_card(WALLED, {
        "v": 1, "provider": WALLED, "window": "monthly",
        "closed_at": int(time.time()), "reset_at": reset_at, "opened_at": None,
    }, board=board)
    if release_all is not None:
        result = kb.set_quota_gate_release_all(release_all, board=board)
        assert result["ok"], result
    return reset_at


def _card_body(board):
    path = kb._quota_gate_db_path(board)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = kb._load_gate_card(conn)
        return json.loads(row["body"]) if row and row["body"] else None
    finally:
        conn.close()


def _events(conn, task_id, kind):
    return [
        json.loads(row["payload"]) if row["payload"] else {}
        for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, kind),
        )
    ]


# ── 1. the switch rides the card, and a refresh cannot revert it ────────────


def test_reader_surfaces_the_switch_from_the_card(gate_board):
    _close_gate(None, gate_board, release_all=True)

    state = kb.read_quota_gate_state(enabled=True)

    assert state.is_closed and state.release_all is True


def test_a_card_without_the_key_reads_as_hold(gate_board):
    """The default must be HOLD, not "absent means release"."""
    _close_gate(None, gate_board)

    state = kb.read_quota_gate_state(enabled=True)

    assert state.is_closed and state.release_all is False
    assert _card_body(gate_board).get("release_all") is None


def test_a_429_refresh_preserves_the_switch(gate_board):
    """The trigger rebuilds the body from scratch; the switch must survive it.

    Without the carry, the operator turns release-all on, the next 429 refresh (or a
    ``hermes quota close``) rewrites the card, and the fleet silently starts holding
    again with nothing in the logs to explain it.
    """
    _close_gate(None, gate_board, release_all=True)
    from agent import quota_gate_trigger as trigger

    later = time.time() + 12 * 86400
    assert trigger.maybe_close_gate(provider=WALLED, reset_at=later, reason="billing") is True

    body = _card_body(gate_board)
    assert body["release_all"] is True        # carried through the refresh
    assert body["reset_at"] == int(later)     # ...while the incident state advanced


def test_the_switch_survives_the_other_writers_too(gate_board):
    """``hermes quota close`` and the probe's re-close go through the same carry."""
    _close_gate(None, gate_board, release_all=True)

    kb.write_quota_gate_card(WALLED, {
        "v": 1, "provider": WALLED, "window": "5h",
        "closed_at": int(time.time()), "reset_at": int(time.time()) + 3600, "opened_at": None,
    }, board=gate_board)

    assert kb.read_quota_gate_state(enabled=True).release_all is True


def test_a_fresh_wall_starts_in_hold(gate_board):
    """Per-incident semantics: the switch dies with the card, so a new wall holds."""
    _close_gate(None, gate_board, release_all=True)
    assert kb.delete_quota_gate_card(WALLED, board=gate_board) is True

    _close_gate(None, gate_board)  # a new wall, new card

    assert kb.read_quota_gate_state(enabled=True).release_all is False


def test_toggle_round_trips_on_the_card(gate_board):
    _close_gate(None, gate_board)

    assert kb.set_quota_gate_release_all(True, board=gate_board)["ok"] is True
    assert kb.read_quota_gate_state(enabled=True).release_all is True
    assert _card_body(gate_board)["release_all"] is True

    assert kb.set_quota_gate_release_all(False, board=gate_board)["ok"] is True
    assert kb.read_quota_gate_state(enabled=True).release_all is False


def test_toggle_with_no_wall_is_not_a_silent_success(gate_board):
    """There is nothing to switch while the gate is open — say so, and fail."""
    result = kb.set_quota_gate_release_all(True, board=gate_board)

    assert result["ok"] is False
    assert result["reason"] == "no_gate_card"


def test_toggle_on_a_missing_board_is_reported(gate_board, monkeypatch):
    monkeypatch.setattr(kb, "_quota_gate_config", lambda: (True, "gate-absent"))

    result = kb.set_quota_gate_release_all(True)

    assert result["ok"] is False and result["reason"] == "board_not_found"


def test_toggle_preserves_the_rest_of_the_card(gate_board):
    """It patches one key; it must not rewrite the incident state it found."""
    reset_at = _close_gate(None, gate_board)

    kb.set_quota_gate_release_all(True, board=gate_board)

    state = kb.read_quota_gate_state(enabled=True)
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
    assert kb.read_quota_gate_state(enabled=True).release_all is True


def test_cli_release_all_off_returns_to_hold(gate_board, monkeypatch, capsys):
    _close_gate(None, gate_board, release_all=True)
    from hermes_cli import quota as quota_cli

    rc = quota_cli.quota_command(argparse.Namespace(action="release-all", state="off"))

    assert rc == 0
    assert "Gate mode: ON" in capsys.readouterr().out
    assert kb.read_quota_gate_state(enabled=True).release_all is False


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
