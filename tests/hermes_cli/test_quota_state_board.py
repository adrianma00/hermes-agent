"""The gate writer and reader must agree on state — now via local state DB.

Phase 2B: the board card writer/reader tests are replaced with local state DB
tests.  ``quota_state`` is the source of truth for all quota gate operations.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from hermes_cli.quota_state import (
    QuotaGateState,
    write_wall,
    delete_wall,
    read_state,
    set_release_all,
    sweep_expired,
    list_walls,
    get_db_path,
)


def _raw_wall_count() -> int:
    """Rows physically on file, expired or not (``list_walls`` hides expired)."""
    conn = sqlite3.connect(str(get_db_path()))
    try:
        return conn.execute("SELECT COUNT(*) FROM wall_state").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean_db():
    """Start each test with a fresh state DB."""
    db = get_db_path()
    if db.exists():
        db.unlink()
    yield
    if db.exists():
        db.unlink()


def test_write_and_read_wall():
    """Writing a wall makes the gate read as closed."""
    now = time.time()
    write_wall("custom:modelark", reset_at=now + 3600, window="5h")
    state = read_state()
    assert state.is_closed is True
    assert state.walled_provider == "custom:modelark"
    assert state.window == "5h"


def test_multi_provider_walls():
    """Multiple walled providers: gate is closed, primary is latest reset."""
    now = time.time()
    write_wall("p1", reset_at=now + 100)
    write_wall("p2", reset_at=now + 200)
    write_wall("p3", reset_at=now + 50)
    state = read_state()
    assert state.is_closed is True
    assert state.walled_provider == "p2"  # latest reset_at
    for p in ("p1", "p2", "p3"):
        delete_wall(p)


def test_expired_wall_is_open():
    """A wall whose reset_at has passed reads as open."""
    write_wall("expired", reset_at=time.time() - 10)
    state = read_state()
    assert state.is_closed is False


def test_delete_wall():
    """Deleting a wall opens the gate."""
    write_wall("test", reset_at=time.time() + 3600)
    assert read_state().is_closed is True
    delete_wall("test")
    assert read_state().is_closed is False


def test_sweep_expired():
    """Sweep removes expired walls (the reader already hides them)."""
    write_wall("a", reset_at=time.time() - 100)
    write_wall("b", reset_at=time.time() + 3600)
    # ``list_walls`` hides the expired row, but both are still on file.
    assert len(list_walls()) == 1
    assert _raw_wall_count() == 2
    swept = sweep_expired()
    assert swept == 1
    assert len(list_walls()) == 1
    assert _raw_wall_count() == 1
    delete_wall("b")


def test_release_all_per_provider():
    """release_all is per-provider and reflected in the aggregate state."""
    now = time.time()
    write_wall("p1", reset_at=now + 100)
    write_wall("p2", reset_at=now + 200)
    set_release_all("p2", True)
    state = read_state()
    assert state.release_all is True
    delete_wall("p1")
    delete_wall("p2")


def test_fallback_chain():
    """Fallback chain populates fallback_model/fallback_provider."""
    write_wall("custom", reset_at=time.time() + 3600)
    state = read_state(fallback_chain=[{"provider": "deepseek", "model": "deepseek-v4-flash"}])
    assert state.fallback_provider == "deepseek"
    assert state.fallback_model == "deepseek-v4-flash"
    delete_wall("custom")


def test_is_exempt():
    """Different provider is exempt; same provider is not."""
    gs = QuotaGateState(is_closed=True, walled_provider="custom:modelark")
    assert gs.is_exempt("deepseek") is True
    assert gs.is_exempt("custom:modelark") is False
    assert gs.is_exempt(None) is False


def test_skip_spawn():
    """skip_spawn is True when closed with no fallback."""
    assert QuotaGateState(is_closed=True).skip_spawn is True
    assert QuotaGateState(is_closed=True, fallback_model="m", fallback_provider="p").skip_spawn is False
    assert QuotaGateState(is_closed=False).skip_spawn is False


def test_open_gate():
    """No walls = open gate."""
    state = read_state()
    assert state.is_closed is False
