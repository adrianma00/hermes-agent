"""Proper pytest suite for ``hermes_cli/quota_state.py``.

Covers the local SQLite-backed quota gate state store end to end: wall
read/write/delete/sweep, the multi-provider aggregate, the ``release_all``
switch, the cost-window bypass, and the pure ``QuotaGateState`` predicates
(``skip_spawn`` / ``is_exempt``).

Every test runs against a throwaway DB: an autouse fixture patches
``get_db_path`` to a ``tmp_path`` file, so nothing here ever touches the
operator's real ``~/.hermes/quota_state.db``.  This replaces the old
``test_quota_state_smoke.py`` standalone script — same coverage, collected
and asserted by pytest, plus the parameterization and edge cases the script
could not express.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import quota_state
from hermes_cli.quota_state import (
    QuotaGateState,
    clear_bypass,
    delete_wall,
    list_walls,
    read_bypass,
    read_state,
    set_release_all,
    sweep_expired,
    write_bypass,
    write_wall,
)


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch, tmp_path: Path) -> Path:
    """Redirect every ``quota_state`` call at a per-test temp DB.

    Patches the module attribute (not the test module's imported name) so the
    patch is seen by ``_connect`` as well as by anything reading
    ``quota_state.get_db_path`` directly.
    """
    test_db = tmp_path / "quota_state.db"
    monkeypatch.setattr(quota_state, "get_db_path", lambda: test_db)
    return test_db


# ── DB bootstrap & edge cases ───────────────────────────────────────────


def test_no_walls_is_open() -> None:
    state = read_state()
    assert state.is_closed is False
    assert state.walled_provider is None
    assert state.release_all is False


def test_an_empty_db_file_is_open(_fresh_db: Path) -> None:
    """A zero-byte file is a valid empty SQLite DB — schema is created lazily."""
    _fresh_db.write_bytes(b"")
    assert read_state().is_closed is False
    assert list_walls() == []


def test_a_corrupt_db_fails_closed(_fresh_db: Path) -> None:
    """Unreadable state must HOLD the fleet, never raise into the caller."""
    _fresh_db.write_bytes(b"this is not a sqlite database" * 64)

    state = read_state()

    assert state.is_closed is True
    assert state.walled_provider is None


def test_a_missing_db_parent_dir_is_created(monkeypatch, tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "quota_state.db"
    monkeypatch.setattr(quota_state, "get_db_path", lambda: nested)

    now = time.time()
    write_wall("p", reset_at=now + 3600)
    assert nested.exists()

    state = read_state()
    assert state.is_closed is True
    assert state.walled_provider == "p"


def test_wal_journal_mode(_fresh_db: Path) -> None:
    """The store is created in WAL mode so readers don't block the writer."""
    write_wall("p", reset_at=time.time() + 3600)  # forces the DB to exist

    conn = sqlite3.connect(str(_fresh_db))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode == "wal"


# ── Wall read / write / delete ──────────────────────────────────────────


def test_write_and_read_wall() -> None:
    write_wall("custom:modelark", reset_at=time.time() + 3600, window="5h")
    state = read_state()
    assert state.is_closed is True
    assert state.walled_provider == "custom:modelark"
    assert state.window == "5h"


def test_write_wall_overwrites_the_same_provider() -> None:
    now = time.time()
    write_wall("p", reset_at=now + 100)
    write_wall("p", reset_at=now + 500)
    walls = list_walls()
    assert len(walls) == 1
    assert walls[0]["reset_at"] == pytest.approx(now + 500)


def test_expired_wall_is_open() -> None:
    write_wall("expired", reset_at=time.time() - 10)
    assert read_state().is_closed is False


def test_delete_wall() -> None:
    write_wall("test", reset_at=time.time() + 3600)
    assert read_state().is_closed is True
    assert delete_wall("test") is True
    assert read_state().is_closed is False


def test_delete_nonexistent_is_false() -> None:
    assert delete_wall("ghost") is False


# ── Expired-wall sweep (parameterized) ──────────────────────────────────


@pytest.mark.parametrize(
    "offsets, expected_swept",
    [
        ([-100, 3600], 1),
        ([-100, -200], 2),
        ([3600, 7200], 0),
        ([], 0),
    ],
)
def test_sweep_expired(offsets: list[float], expected_swept: int) -> None:
    now = time.time()
    for i, off in enumerate(offsets):
        write_wall(f"w{i}", reset_at=now + off)

    assert sweep_expired(now) == expected_swept
    assert len(list_walls()) == len(offsets) - expected_swept


def test_sweep_expired_at_exactly_now() -> None:
    """``reset_at == now`` is expired (the DELETE uses ``<=``)."""
    now = time.time()
    write_wall("edge", reset_at=now)
    assert sweep_expired(now) == 1


# ── Multi-provider aggregate (parameterized) ────────────────────────────


@pytest.mark.parametrize(
    "walls, release_on, expected_primary, expected_release_all",
    [
        ([("p1", 100), ("p2", 200), ("p3", 50)], None, "p2", False),
        ([("p1", 100), ("p2", 200)], "p2", "p2", True),
        # release_all is "any provider", but the primary stays the latest reset.
        ([("p1", 100), ("p2", 200)], "p1", "p2", True),
        ([("p1", 100), ("p2", 200), ("p3", 50)], "p3", "p2", True),
        ([("only", 100)], None, "only", False),
    ],
)
def test_multi_provider_aggregate(
    walls: list[tuple[str, float]],
    release_on: str | None,
    expected_primary: str,
    expected_release_all: bool,
) -> None:
    now = time.time()
    for provider, off in walls:
        write_wall(provider, reset_at=now + off)
    if release_on is not None:
        assert set_release_all(release_on, True) is True

    state = read_state()

    assert state.is_closed is True
    assert state.walled_provider == expected_primary
    assert state.release_all is expected_release_all


def test_multi_provider_ignores_an_expired_wall() -> None:
    now = time.time()
    write_wall("alive", reset_at=now + 100)
    write_wall("dead", reset_at=now - 100)

    state = read_state()

    assert state.walled_provider == "alive"
    assert len(list_walls()) == 1


# ── release_all switch (parameterized) ──────────────────────────────────


@pytest.mark.parametrize(
    "exists, enabled, expected_return, expected_flag",
    [
        (True, True, True, True),
        (True, False, True, False),
        (False, True, False, False),
    ],
)
def test_set_release_all(
    exists: bool, enabled: bool, expected_return: bool, expected_flag: bool
) -> None:
    if exists:
        write_wall("p", reset_at=time.time() + 3600)

    assert set_release_all("p", enabled) is expected_return
    assert read_state().release_all is expected_flag


@pytest.mark.parametrize("enabled", [True, False])
def test_release_all_is_false_without_any_wall(enabled: bool) -> None:
    write_wall("p", reset_at=time.time() + 3600)
    write_wall("q", reset_at=time.time() - 10)  # expired
    set_release_all("q", enabled)
    assert read_state().release_all is False


# ── release_all survives a refresh, dies with the wall ──────────────────


def test_write_wall_carries_release_all_through_a_live_refresh() -> None:
    """A 429 re-close / ``quota close`` rewrites the wall, not the operator's switch.

    The writer rebuilds no body: a still-live wall refreshes its ``reset_at`` while the
    ``release_all`` on file is preserved, so the operator's decision survives the writers
    that re-close the gate.
    """
    now = time.time()
    write_wall("p", reset_at=now + 3600)
    set_release_all("p", True)

    write_wall("p", reset_at=now + 7200, window="monthly")  # a refresh

    assert read_state().release_all is True
    assert read_state().reset_at == pytest.approx(now + 7200)


def test_write_wall_resets_release_all_for_a_new_wall() -> None:
    """A wall that ENDED does not carry its switch into the next one.

    The expired row is still on file (nothing sweeps it on a tick), so the writer must
    treat the rewrite as a new incident and start it in HOLD.
    """
    now = time.time()
    write_wall("p", reset_at=now - 10)  # already expired
    set_release_all("p", True)
    assert read_state().release_all is False  # the expired wall governs nothing

    write_wall("p", reset_at=now + 3600)  # a genuinely new wall

    assert read_state().release_all is False


def test_write_wall_explicit_flag_overrides_the_carry() -> None:
    write_wall("p", reset_at=time.time() + 3600)
    set_release_all("p", True)

    write_wall("p", reset_at=time.time() + 7200, release_all=False)

    assert read_state().release_all is False


def test_write_wall_carry_is_per_provider() -> None:
    now = time.time()
    write_wall("p", reset_at=now + 3600)
    write_wall("q", reset_at=now + 3600)
    set_release_all("p", True)

    write_wall("q", reset_at=now + 7200)  # refresh q only

    assert read_state().release_all is True  # p still released
    walls = {w["provider"]: w for w in list_walls()}
    assert walls["q"]["release_all"] == 0


# ── Fallback chain ──────────────────────────────────────────────────────


def test_fallback_chain_populates_route() -> None:
    write_wall("custom", reset_at=time.time() + 3600)
    state = read_state(
        fallback_chain=[{"provider": "deepseek", "model": "deepseek-v4-flash"}]
    )
    assert state.fallback_provider == "deepseek"
    assert state.fallback_model == "deepseek-v4-flash"


@pytest.mark.parametrize("chain", [None, [], "not-a-list"])
def test_no_fallback_chain_leaves_route_unset(chain) -> None:
    write_wall("custom", reset_at=time.time() + 3600)
    state = read_state(fallback_chain=chain)
    assert state.fallback_model is None
    assert state.fallback_provider is None


# ── QuotaGateState predicates (parameterized) ───────────────────────────


@pytest.mark.parametrize(
    "walled, override, expected",
    [
        ("custom:modelark", "deepseek", True),
        ("custom:modelark", "custom:modelark", False),
        ("Custom:ModelArk", "custom:modelark", False),  # case-insensitive match
        ("custom:modelark", None, False),
        ("custom:modelark", "", False),
        ("custom:modelark", "   ", False),
        (None, "deepseek", True),
        (None, None, False),
    ],
)
def test_is_exempt(walled, override, expected: bool) -> None:
    gate = QuotaGateState(is_closed=True, walled_provider=walled)
    assert gate.is_exempt(override) is expected


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"is_closed": True}, True),
        ({"is_closed": True, "fallback_model": "m", "fallback_provider": "p"}, False),
        ({"is_closed": False}, False),
        ({"is_closed": False, "fallback_model": "m"}, False),
    ],
)
def test_skip_spawn(kwargs: dict, expected: bool) -> None:
    assert QuotaGateState(**kwargs).skip_spawn is expected


# ── Cost-window bypass (parameterized) ──────────────────────────────────


@pytest.mark.parametrize("offset, alive", [(7200, True), (-10, False)])
def test_bypass_roundtrip(offset: float, alive: bool) -> None:
    write_bypass(time.time() + offset)
    got = read_bypass()
    if alive:
        assert got is not None
    else:
        assert got is None


def test_no_bypass_is_none() -> None:
    assert read_bypass() is None


def test_clear_bypass() -> None:
    write_bypass(time.time() + 7200)
    clear_bypass()
    assert read_bypass() is None


def test_clear_bypass_is_idempotent() -> None:
    clear_bypass()
    clear_bypass()  # must not raise


def test_write_bypass_extends() -> None:
    now = time.time()
    write_bypass(now + 100)
    write_bypass(now + 500)
    assert read_bypass() == pytest.approx(now + 500)


# ── Concurrent access ───────────────────────────────────────────────────


def test_concurrent_readers() -> None:
    """Many readers see the same closed gate (WAL read concurrency)."""
    write_wall("test", reset_at=time.time() + 3600)

    results: list[bool] = []
    lock = threading.Lock()

    def reader() -> None:
        state = read_state()
        with lock:
            results.append(state.is_closed)

    threads = [threading.Thread(target=reader) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [True] * 10


@pytest.mark.parametrize("n", [2, 8])
def test_concurrent_writers_begin_immediate(n: int) -> None:
    """Parallel writers contend on ``BEGIN IMMEDIATE`` without error or loss."""
    now = time.time()
    errors: list[str] = []
    lock = threading.Lock()

    def writer(i: int) -> None:
        try:
            write_wall(f"p{i}", reset_at=now + 100 + i)
        except Exception as exc:  # noqa: BLE001 - surface the real failure
            with lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(list_walls()) == n


def test_concurrent_writers_and_readers() -> None:
    """A reader racing a writer never sees a torn/absent DB."""
    errors: list[str] = []
    lock = threading.Lock()

    def writer(i: int) -> None:
        for _ in range(5):
            try:
                write_wall(f"p{i}", reset_at=time.time() + 3600)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(repr(exc))

    def reader() -> None:
        for _ in range(10):
            try:
                read_state()
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
