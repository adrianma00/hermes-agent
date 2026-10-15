"""Per-install SQLite-backed state store for the provider-quota gate and cost-window bypass.

Replaces the shared-board card mechanism: one local DB per install
(``~/.hermes/quota_state.db``) holds all walled providers with per-provider
``release_all`` flags and the cost-window bypass.  WAL + ``BEGIN IMMEDIATE``
for safe concurrent access from gateway, workers, and CLI.

Phase 1A: dual-write (card + local DB).  Readers still consume the card.
Phase 2A: readers flip to this module; card machinery decommissioned.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class QuotaGateState:
    """Whether the provider-quota gate is closed and how to route overrides.

    Defined here in Phase 1A (moved from ``kanban_db.py``) so the state module
    has zero imports from the heavy kanban_db import chain.  Phase 2A:
    ``kanban_db.py`` imports this class from *us*.
    """

    def __init__(
        self,
        is_closed: bool = False,
        reset_at: Optional[float] = None,
        walled_provider: Optional[str] = None,
        window: Optional[str] = None,
        fallback_model: Optional[str] = None,
        fallback_provider: Optional[str] = None,
        release_all: bool = False,
    ):
        self.is_closed = is_closed
        self.reset_at = reset_at
        self.walled_provider = walled_provider
        self.window = window
        self.fallback_model = fallback_model
        self.fallback_provider = fallback_provider
        self.release_all = release_all

    @property
    def skip_spawn(self) -> bool:
        """True when the gate is closed and no fallback routing is available
        (hard pause — tasks without ``quota_override`` must not spawn)."""
        return self.is_closed and not self.fallback_model

    def is_exempt(self, provider_override: Optional[str]) -> bool:
        """Whether a card pinned to ``provider_override`` sidesteps the gate.

        Only an explicit pin to a provider OTHER than the walled one is
        exempt: a card pinned to the walled provider itself still waits.
        """
        pinned = (provider_override or "").strip().lower()
        walled = (self.walled_provider or "").strip().lower()
        return bool(pinned) and pinned != walled

    def __repr__(self) -> str:
        return (
            f"QuotaGateState(is_closed={self.is_closed}, "
            f"reset_at={self.reset_at}, "
            f"walled_provider={self.walled_provider!r}, "
            f"window={self.window!r}, "
            f"fallback_model={self.fallback_model!r}, "
            f"fallback_provider={self.fallback_provider!r}, "
            f"release_all={self.release_all})"
        )


_SCHEMA_VERSION = 1
_DB_FILENAME = "quota_state.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wall_state (
    provider     TEXT PRIMARY KEY,
    window       TEXT,
    reset_at     REAL NOT NULL,
    closed_at    REAL NOT NULL,
    source       TEXT DEFAULT 'first-429',
    release_all  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cost_window_bypass (
    key          TEXT PRIMARY KEY DEFAULT 'bypass',
    bypass_until REAL NOT NULL
);

PRAGMA journal_mode=WAL;
"""


# ── Connection helpers ─────────────────────────────────────────────────


def get_db_path() -> Path:
    """Resolve the quota state DB path, per-install under HERMES_HOME."""
    from hermes_constants import get_hermes_home  # type: ignore[import-untyped]

    return Path(get_hermes_home()) / _DB_FILENAME


def _connect() -> sqlite3.Connection:
    """Open a connection to the quota state DB, creating schema if needed."""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    _enable_wal(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _enable_wal(conn: sqlite3.Connection, attempts: int = 50, delay: float = 0.01) -> None:
    """Switch the connection's database to WAL, tolerating a benign race.

    WAL is a persistent DB property, so this is only a real mode change on the
    first creation — but a bare ``PRAGMA journal_mode=WAL`` needs a brief
    exclusive lock and returns ``SQLITE_BUSY`` *immediately* (it does not honour
    the busy handler) when another connection holds the database.  That happens
    whenever the gateway, a worker and the CLI open the DB together, and it made
    ``write_wall`` raise ``OperationalError('database is locked')`` out of
    ``_connect``.  Retry a bounded number of times before giving up.
    """
    for attempt in range(attempts):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist.  Idempotent."""
    conn.executescript(_SCHEMA_SQL)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(_SCHEMA_VERSION),),
    )
    conn.commit()


# ── Quota gate wall state ──────────────────────────────────────────────


def read_state(
    fallback_chain: Optional[list[dict]] = None,
) -> QuotaGateState:
    """Read the aggregate gate state from all walled providers.

    Returns a :class:`QuotaGateState` built from every non-expired wall row.
    The gate is closed when ANY provider is walled; ``walled_provider`` is the
    one with the latest ``reset_at``; ``release_all`` is True when ANY walled
    provider has its flag set.

    ``fallback_chain`` populates ``fallback_model``/``fallback_provider``
    (same semantics as the card reader in ``kanban_db.py``).
    """
    conn = None
    try:
        conn = _connect()
        now = time.time()
        rows = conn.execute(
            "SELECT provider, window, reset_at, source, release_all "
            "FROM wall_state WHERE reset_at > ? ORDER BY reset_at DESC",
            (now,),
        ).fetchall()

        if not rows:
            return QuotaGateState(is_closed=False)

        # Primary wall = the one with the latest reset_at
        primary = rows[0]
        # release_all is True if ANY walled provider has it set
        any_release = any(bool(r["release_all"]) for r in rows)

        fb_model = None
        fb_provider = None
        if isinstance(fallback_chain, list) and fallback_chain:
            fb_model = fallback_chain[0].get("model")
            fb_provider = fallback_chain[0].get("provider")

        return QuotaGateState(
            is_closed=True,
            reset_at=float(primary["reset_at"]),
            walled_provider=primary["provider"],
            window=primary["window"],
            fallback_model=fb_model,
            fallback_provider=fb_provider,
            release_all=any_release,
        )
    except Exception as exc:
        logger.warning("Quota state unreadable — holding the fleet: %s", exc)
        return QuotaGateState(is_closed=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def write_wall(
    provider: str,
    *,
    window: str = "5h",
    reset_at: float,
    closed_at: Optional[float] = None,
    source: str = "first-429",
    release_all: Optional[bool] = None,
) -> None:
    """Record or update a wall for *provider*.

    Uses ``INSERT OR REPLACE`` so a new wall for the same provider overwrites
    the previous one.  Only the latest ``reset_at`` matters.

    ``release_all`` defaults to ``None``, which means *carry the operator's
    switch through a refresh*: when a still-live wall is rewritten (a 429
    re-close, ``hermes quota close``, a probe), the flag already on file for the
    provider is preserved, so the operator's decision survives the writers that
    rebuild the wall.  A genuinely NEW wall — no row on file, or an
    already-expired one — always starts in HOLD, which is the property the
    switch's placement on the wall exists to guarantee: it dies with the wall
    rather than governing a later one against a different reset distance.  Pass
    an explicit ``True``/``False`` to force the flag.
    """
    conn = None
    try:
        conn = _connect()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if release_all is None:
                existing = conn.execute(
                    "SELECT reset_at, release_all FROM wall_state WHERE provider = ?",
                    (provider,),
                ).fetchone()
                # Carry only off a wall that is still in force.  Once the reset
                # has passed the row describes a wall that ENDED; a subsequent
                # write is a new incident and must not inherit its switch.
                if existing is not None and float(existing["reset_at"]) > time.time():
                    release_all = bool(existing["release_all"])
                else:
                    release_all = False
            conn.execute(
                """INSERT OR REPLACE INTO wall_state
                   (provider, window, reset_at, closed_at, source, release_all)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    provider,
                    window,
                    float(reset_at),
                    float(closed_at) if closed_at else time.time(),
                    source,
                    1 if release_all else 0,
                ),
            )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def delete_wall(provider: str) -> bool:
    """Remove the wall for *provider*.  Returns True when a row was deleted."""
    conn = None
    try:
        conn = _connect()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "DELETE FROM wall_state WHERE provider = ?", (provider,)
            )
            return cursor.rowcount > 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def set_release_all(provider: str, enabled: bool) -> bool:
    """Set the ``release_all`` flag for a walled provider.

    Returns True when the row exists (flag was actually set).
    Returns False when no wall exists for that provider.
    """
    conn = None
    try:
        conn = _connect()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE wall_state SET release_all = ? WHERE provider = ?",
                (1 if enabled else 0, provider),
            )
            return cursor.rowcount > 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def sweep_expired(now: Optional[float] = None) -> int:
    """Remove all walls whose ``reset_at`` has passed.

    Returns the count of rows removed.
    """
    if now is None:
        now = time.time()
    conn = None
    try:
        conn = _connect()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "DELETE FROM wall_state WHERE reset_at <= ?", (float(now),)
            )
            return cursor.rowcount
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def list_walls() -> list[dict]:
    """Return all non-expired walls as dicts (for diagnostics)."""
    conn = None
    try:
        conn = _connect()
        now = time.time()
        rows = conn.execute(
            "SELECT provider, window, reset_at, closed_at, source, release_all "
            "FROM wall_state WHERE reset_at > ? ORDER BY reset_at DESC",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── Cost-window bypass ─────────────────────────────────────────────────


def read_bypass() -> Optional[float]:
    """Read the cost-window bypass ``bypass_until``.

    Returns the epoch timestamp if set and not yet expired, else None.
    """
    conn = None
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT bypass_until FROM cost_window_bypass WHERE key = 'bypass'"
        ).fetchone()
        if row is None:
            return None
        until = float(row["bypass_until"])
        return until if until > time.time() else None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def write_bypass(until_epoch: float) -> None:
    """Set or extend the cost-window bypass until *until_epoch*."""
    conn = None
    try:
        conn = _connect()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO cost_window_bypass (key, bypass_until) "
                "VALUES ('bypass', ?)",
                (float(until_epoch),),
            )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def clear_bypass() -> None:
    """Remove the cost-window bypass (idempotent)."""
    conn = None
    try:
        conn = _connect()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM cost_window_bypass WHERE key = 'bypass'"
            )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
