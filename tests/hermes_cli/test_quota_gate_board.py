"""The gate writer must land on the same board the reader reads.

The reader has always honoured ``kanban.quota_gate.board``; the writer resolved a
module constant instead.  On any install that sets the key, the writer closed the
gate on a board nothing read (or failed outright when that board did not exist),
the trigger's ``except`` swallowed it, and the dispatcher kept feeding the walled
provider.  These tests pin writer/reader/CLI symmetry.

The second half pins the OVERRIDE ROUTE symmetry: the board slug has one resolver, and so
does the fallback chain (``kanban_db.quota_gate_fallback_chain``) — a CLI that read no chain
of its own printed "Hard pause: no fallback route" while the dispatcher routed to one.
"""

import argparse
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb


def _patch_config(monkeypatch, board, enabled=True):
    monkeypatch.setattr(kb, "_quota_gate_config", lambda: (enabled, board))
    return board


def _cards(slug):
    """Titles of gate cards on *slug*'s board, read through the same resolution the writer used."""
    path = kb._quota_gate_db_path(slug)
    assert path is not None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [row[0] for row in conn.execute(
            "SELECT title FROM tasks WHERE title LIKE 'quota-gate:%'")]
    finally:
        conn.close()


def _body(reset_at=None):
    now = int(time.time())
    return {"v": 1, "provider": "custom", "window": "weekly",
            "closed_at": now, "reset_at": reset_at or now + 3600, "opened_at": None}


def test_writer_lands_on_the_configured_board(monkeypatch):
    kb.create_board("gate-a")
    kb.create_board("gate-b")
    _patch_config(monkeypatch, "gate-a")

    kb.write_quota_gate_card("custom", _body())

    assert _cards("gate-a") == ["quota-gate:custom"]
    assert _cards("gate-b") == []


def test_explicit_board_argument_wins_over_config(monkeypatch):
    kb.create_board("gate-a")
    kb.create_board("gate-b")
    _patch_config(monkeypatch, "gate-a")

    kb.write_quota_gate_card("custom", _body(), board="gate-b")

    assert _cards("gate-b") == ["quota-gate:custom"]
    assert _cards("gate-a") == []


def test_writer_without_a_configured_board_uses_the_default(monkeypatch):
    kb.create_board(kb._QUOTA_GATE_BOARD)
    _patch_config(monkeypatch, kb._QUOTA_GATE_BOARD)

    kb.write_quota_gate_card("custom", _body())

    assert _cards(kb._QUOTA_GATE_BOARD) == ["quota-gate:custom"]


def test_a_missing_board_raises_naming_the_slug_it_looked_for(monkeypatch):
    _patch_config(monkeypatch, "gate-missing")

    with pytest.raises(RuntimeError, match="gate-missing"):
        kb.write_quota_gate_card("custom", _body())


def test_open_follows_the_same_board_as_close(monkeypatch):
    kb.create_board("gate-a")
    kb.create_board("gate-b")
    kb.write_quota_gate_card("custom", _body(), board="gate-a")

    _patch_config(monkeypatch, "gate-b")
    assert kb.delete_quota_gate_card("custom") is False  # a different board has no card

    _patch_config(monkeypatch, "gate-a")
    assert kb.delete_quota_gate_card("custom") is True
    assert _cards("gate-a") == []


def test_trigger_passes_the_configured_board_to_the_writer(monkeypatch):
    kb.create_board("gate-a")
    _patch_config(monkeypatch, "gate-a")
    from agent import quota_gate_trigger as trigger

    seen = {}

    def _capture(provider, body, **kwargs):
        seen["provider"] = provider
        seen["board"] = kwargs.get("board")

    monkeypatch.setattr(kb, "write_quota_gate_card", _capture)

    closed = trigger.maybe_close_gate(provider="custom", reset_at=time.time() + 3600, reason="billing")

    assert closed is True
    assert seen["provider"] == "custom"
    assert seen["board"] == "gate-a"  # not the module constant


# --- the override route: one resolver, so the CLI cannot contradict the dispatcher -------------

def _closed_gate(monkeypatch, board="gate-route"):
    """A board carrying a future-reset gate card, selected as the gate board."""
    kb.create_board(board)
    _patch_config(monkeypatch, board)
    kb.write_quota_gate_card("custom", _body(reset_at=int(time.time()) + 7 * 86400), board=board)
    return board


def _config_with(*entries, legacy=None):
    cfg = {"fallback_providers": list(entries)}
    if legacy is not None:
        cfg["fallback_model"] = legacy
    return cfg


def test_status_reports_the_override_route_the_dispatcher_would_use(monkeypatch, capsys):
    """The closed gate must not claim a hard pause while a fallback route exists."""
    _closed_gate(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: _config_with({"provider": "deepseek", "model": "deepseek-v4-flash"}),
    )
    from hermes_cli import quota as quota_cli

    assert quota_cli._cmd_status(argparse.Namespace()) == 0

    out = capsys.readouterr().out
    assert "Override route: deepseek:deepseek-v4-flash" in out
    assert "Hard pause" not in out


def test_status_says_hard_pause_when_no_route_exists(monkeypatch, capsys):
    _closed_gate(monkeypatch)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    from hermes_cli import quota as quota_cli

    quota_cli._cmd_status(argparse.Namespace())

    assert "Hard pause: no fallback route" in capsys.readouterr().out


def test_cli_cron_and_dispatcher_resolve_one_chain(monkeypatch):
    """All three readers of the override route agree, including the legacy key order."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: _config_with(
            {"provider": "deepseek", "model": "deepseek-v4-flash"},
            legacy={"provider": "openrouter", "model": "legacy/model"},
        ),
    )
    from cron import scheduler_quota as sq
    from hermes_cli import kanban_db_dispatch as kbd

    shared = kb.quota_gate_fallback_chain()
    assert shared is not None
    assert [e["provider"] for e in shared] == ["deepseek", "openrouter"]
    assert kbd._resolve_fallback_chain() == shared
    assert sq._config_and_chain()[1] == shared


def test_no_configured_route_resolves_to_none(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"fallback_providers": []})

    assert kb.quota_gate_fallback_chain() is None