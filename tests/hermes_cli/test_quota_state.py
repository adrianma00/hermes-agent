"""Proper pytest suite for hermes_cli/quota_state.py.

Tests the local SQLite-backed quota gate state store.
Uses tmp_path to isolate each test's DB.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli.quota_state import (
    QuotaGateState,
    write_wall,
    delete_wall,
    read_state,
    set_release_all,
    sweep_expired,
    list_walls,
    write_bypass,
    read_bypass,
    clear_bypass,
    get_db_path,
)


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch, tmp_path: Path) -> None:
    """Redirect quota_state to a temp DB for each test."""
    test_db = tmp_path / "quota_state.db"
    monkeypatch.setattr("hermes_cli.quota_state.get_db_path", lambda: test_db)
    yield
    if test_db.exists():
        test_db.unlink()


# ── Wall state ──────────────────────────────────────────────────────────


def test_write_and_read_wall() -> None:
    write_wall("custom:modelark", reset_at=time.time() + 3600, window="5h")
    state = read_state()
    assert state.is_closed is True
    assert state.walled_provider == "custom:modelark"
    assert state.window == "5h"


def test_no_walls_is_open() -> None:
    state = read_state()
    assert state.is_closed is False


def test_expired_wall_is_open() -> None:
    write_wall("expired", reset_at=time.time() - 10)
    state = read_state()
    assert state.is_closed is False


def test_delete_wall() -> None:
    write_wall("test", reset_at=time.time() + 3600)
    assert read_state().is_closed is True
    deleted = delete_wall("test")
    assert deleted is True
    assert read_state().is_closed is False


def test_delete_nonexistent() -> None:
    assert delete_wall("ghost") is False


def test_sweep_expired() -> None:
    write_wall("a", reset_at=time.time() - 100)
    write_wall("b", reset_at=time.time() + 3600)
    assert len(list_walls()) == 2
    swept = sweep_expired()
    assert swept >= 1
    walls = list_walls()
    assert len(walls) == 1
    assert walls[0]["provider"] == "b"
    delete_wall("b")


def test_multi_provider_primary_is_latest() -> None:
    now = time.time()
    write_wall("p1", reset_at=now + 100)
    write_wall("p2", reset_at=now + 200)
    write_wall("p3", reset_at=now + 50)
    state = read_state()
    assert state.walled_provider == "p2"
    for p in ("p1", "p2", "p3"):
        delete_wall(p)


def test_multi_provider_release_all_any() -> None:
    now = time.time()
    write_wall("p1", reset_at=now + 100)
    write_wall("p2", reset_at=now + 200)
    set_release_all("p2", True)
    state = read_state()
    assert state.release_all is True
    delete_wall("p1")
    delete_wall("p2")


def test_set_release_all_nonexistent() -> None:
    assert set_release_all("ghost", True) is False


def test_fallback_chain() -> None:
    write_wall("custom", reset_at=time.time() + 3600)
    state = read_state(fallback_chain=[{"provider": "deepseek", "model": "deepseek-v4-flash"}])
    assert state.fallback_provider == "deepseek"
    assert state.fallback_model == "deepseek-v4-flash"
    delete_wall("custom")


def test_no_fallback_chain() -> None:
    write_wall("custom", reset_at=time.time() + 3600)
    state = read_state()
    assert state.fallback_model is None
    assert state.fallback_provider is None
    delete_wall("custom")


# ── QuotaGateState behaviour ────────────────────────────────────────────


def test_is_exempt_different_provider() -> None:
    gs = QuotaGateState(is_closed=True, walled_provider="custom:modelark")
    assert gs.is_exempt("deepseek") is True


def test_is_exempt_same_provider() -> None:
    gs = QuotaGateState(is_closed=True, walled_provider="custom:modelark")
    assert gs.is_exempt("custom:modelark") is False


def test_is_exempt_no_override() -> None:
    gs = QuotaGateState(is_closed=True, walled_provider="custom:modelark")
    assert gs.is_exempt(None) is False
    assert gs.is_exempt("") is False


def test_is_exempt_case_insensitive() -> None:
    gs = QuotaGateState(is_closed=True, walled_provider="Custom:ModelArk")
    assert gs.is_exempt("custom:modelark") is False
    assert gs.is_exempt("deepseek") is True


def test_skip_spawn_closed_no_fallback() -> None:
    assert QuotaGateState(is_closed=True).skip_spawn is True


def test_skip_spawn_closed_with_fallback() -> None:
    assert QuotaGateState(
        is_closed=True, fallback_model="m", fallback_provider="p"
    ).skip_spawn is False


def test_skip_spawn_open() -> None:
    assert QuotaGateState(is_closed=False).skip_spawn is False


# ── Cost-window bypass ──────────────────────────────────────────────────


def test_write_and_read_bypass() -> None:
    write_bypass(time.time() + 7200)
    assert read_bypass() is not None


def test_expired_bypass_is_none() -> None:
    write_bypass(time.time() - 10)
    assert read_bypass() is None


def test_no_bypass_is_none() -> None:
    assert read_bypass() is None


def test_clear_bypass() -> None:
    write_bypass(time.time() + 7200)
    clear_bypass()
    assert read_bypass() is None


def test_clear_bypass_idempotent() -> None:
    clear_bypass()
    clear_bypass()  # must not raise


# ── Concurrent access ───────────────────────────────────────────────────


def test_wal_mode() -> None:
    """Verify the DB is created with WAL journal mode."""
    import sqlite3
    db = get_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal", f"Expected WAL, got {mode}"
    finally:
        conn.close()
        if db.exists():
            db.unlink()


def test_concurrent_readers() -> None:
    """Multiple readers can read simultaneously (WAL)."""
    import sqlite3, threading
    write_wall("test", reset_at=time.time() + 3600)

    results = []

    def reader():
        s = read_state()
        results.append(s.is_closed)

    threads = [threading.Thread(target=reader) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(results)  # all readers saw closed gate
    delete_wall("test")
