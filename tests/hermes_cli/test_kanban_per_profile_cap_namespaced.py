"""Cross-tick half of the per-profile concurrency cap (kanban card t_f7368e75).

``kanban.max_in_progress_per_profile`` is keyed on the RESOLVED profile: on a
board declaring namespace ``em``, a bare ``default`` card and an ``em:default``
card are the SAME profile and must share one budget. The intra-tick half (both
cards ready, one tick) is covered by
``test_kanban_assignee_namespaces.py::test_per_profile_cap_keyed_on_resolved_profile``.

This module covers the tick AFTER a worker is already running, where the budget
used to be seeded from a DB snapshot grouped by the LITERAL ``assignee`` column.
Two consequences, both asserted here:

* a running ``em:default`` was invisible to its own profile's budget (bare
  ``default`` only worked because its literal string happens to equal the
  resolved profile name), and
* a running bare name on a board belonging to ANOTHER namespace was charged to
  this install's budget for that name.

The review-lane reservation mirrors the same keys, so its half is asserted too:
a review card the lane loop would refuse as per-profile capped must not hold the
ready lane's last slot.

Isolation: the fixture home is built OUTSIDE the platform default root and pinned
with ``HERMES_KANBAN_HOME``, and the resolved paths are asserted before the first
write. ``tempfile.mkdtemp()`` honours ``TMPDIR``, which a Hermes-launched pytest
sets to ``<home>/cache/scratch`` — inside the default root, where
``hermes_constants.get_default_hermes_root()`` collapses onto the NATIVE root and
``kanban_home()`` resolves onto the LIVE boards (tracked by card t_7ecd88e2).
Kept in its own module because that card is reworking the namespace suite's
fixture plumbing.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# The install ticking is namespace ``em``; every board below declares either its
# own namespace (this install's board) or a foreign one (the other install's).
INSTALL_NS = "em"
OWN_BOARD = "em-admin"
FOREIGN_BOARD = "yummi-admin"
FOREIGN_NS = "yummi"

# Pins the live dispatcher injects into every worker: an inherited one identifies
# THAT process's own board, so a fixture keeping it writes its cards into the
# live file however isolated its home is.
_INHERITED_PINS = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT",
)


def _fake_spawn(*args, **kwargs) -> int:
    return 12345


def _fixture_home(prefix: str) -> Path:
    """A fresh home OUTSIDE the platform default root (never the inherited TMPDIR)."""
    native = (Path.home() / ".hermes").resolve()
    for candidate in ("/var/tmp", "/tmp", tempfile.gettempdir()):
        parent = Path(candidate).expanduser()
        try:
            resolved = parent.resolve()
        except OSError:  # pragma: no cover — unreadable/odd mount
            continue
        if resolved == native or native in resolved.parents:
            continue  # inside the default root: the exact collapse being fixed
        if not (parent.is_dir() and os.access(parent, os.W_OK)):
            continue
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent))).resolve()
    raise RuntimeError(f"no writable temp base outside {native}")


@pytest.fixture()
def fleet(monkeypatch):
    """A hermetic install home with ``default``/``alpha``/``reviewer`` profiles.

    One install (``em``) is enough for the cap keying; the foreign side is
    expressed by a board declaring another install's namespace.
    """
    home = _fixture_home("kanban_cap_cross_tick_")
    for prof in ("default", "alpha", "reviewer"):
        pdir = home / "profiles" / prof
        pdir.mkdir(parents=True)
        (pdir / "config.yaml").write_text("{}\n")  # a bare dir is not a profile

    for var in _INHERITED_PINS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_NAMESPACE", INSTALL_NS)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    kbd._reset_namespace_cache()
    # Fail LOUD before writing anything: a home that resolves onto the live root
    # is the leak t_7ecd88e2 owns, and it used to go unnoticed.
    resolved = Path(kb.kanban_home()).resolve()
    assert resolved == home, f"kanban_home() resolves to {resolved}, not {home}"
    for slug in (OWN_BOARD, FOREIGN_BOARD):
        board_db = Path(kb.kanban_db_path(slug)).resolve()
        assert home in board_db.parents, f"board {slug} would be written at {board_db}"

    kb.create_board(slug=OWN_BOARD, name=OWN_BOARD, namespace=INSTALL_NS)
    kb.create_board(slug=FOREIGN_BOARD, name=FOREIGN_BOARD, namespace=FOREIGN_NS)
    kbd._reset_namespace_cache()
    try:
        yield SimpleNamespace(home=home, kb=kb, kbc=kbc, kbd=kbd)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _create(env, board: str, title: str, assignee: str, *, running: bool = False) -> str:
    """A ready card, or one genuinely claimed (``claim_task``) by this install."""
    with env.kbc.connect_closing(board=board) as conn:
        task_id = env.kb.create_task(conn, title=title, assignee=assignee)
        if running:
            assert env.kb.claim_task(conn, task_id) is not None
    return task_id


def _create_in_review(env, board: str, title: str, assignee: str) -> str:
    with env.kbc.connect_closing(board=board) as conn:
        task_id = env.kb.create_task(conn, title=title, assignee=assignee)
        with env.kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
    return task_id


def _tick(env, board: str, **kwargs):
    with env.kbc.connect_closing(board=board) as conn:
        return env.kbd.dispatch_once(conn, spawn_fn=_fake_spawn, board=board, **kwargs)


def _seed(env, board: str) -> dict:
    """The cross-tick budget the next tick would see, as the dispatcher builds it."""
    with env.kbc.connect_closing(board=board) as conn:
        return env.kbd._seed_per_profile_running(conn, board)


# ── the cross-tick budget itself ────────────────────────────────────────────


@pytest.mark.parametrize(
    "first, second",
    [
        ("default", "em:default"),   # bare first: shared the key only by coincidence
        ("em:default", "default"),   # explicit first: used to be INVISIBLE to the cap
    ],
)
def test_running_worker_consumes_the_resolved_profile_budget(fleet, first, second):
    """A running worker blocks its profile no matter which spelling it uses."""
    env = fleet
    first_id = _create(env, OWN_BOARD, f"first-{first}", first, running=True)

    # The seed normalises today's spelling onto the profile the check uses.
    assert _seed(env, OWN_BOARD) == {"default": 1}

    second_id = _create(env, OWN_BOARD, f"second-{second}", second)
    result = _tick(env, OWN_BOARD, max_in_progress_per_profile=1)

    assert result.spawned == [], result.spawned
    assert [t for t, _, _ in result.skipped_per_profile_capped] == [second_id]
    assert first_id != second_id


def test_intra_tick_cap_still_spawns_exactly_one(fleet):
    """The shipped intra-tick guarantee, on this fixture: 1 spawn + 1 capped."""
    env = fleet
    _create(env, OWN_BOARD, "bare", "default")
    _create(env, OWN_BOARD, "explicit", "em:default")
    result = _tick(env, OWN_BOARD, dry_run=True, max_in_progress_per_profile=1)
    assert len(result.spawned) == 1
    assert len(result.skipped_per_profile_capped) == 1


# ── foreign namespaces must not be charged to this install ──────────────────


def test_foreign_boards_running_bare_name_is_not_our_budget(fleet):
    """A running bare ``default`` on the OTHER install's board is not our worker.

    Its literal string is this install's profile name, which is exactly why the
    literal-keyed snapshot charged it to us: with cap=1 our own ``em:default``
    card was refused as capped by a worker this install does not run.
    """
    env = fleet
    _create(env, FOREIGN_BOARD, "their-worker", "default", running=True)
    assert _seed(env, FOREIGN_BOARD) == {}

    ready_id = _create(env, FOREIGN_BOARD, "ours-here", "em:default")
    result = _tick(env, FOREIGN_BOARD, max_in_progress_per_profile=1)
    assert [t for t, _, _ in result.spawned] == [ready_id]
    assert result.skipped_per_profile_capped == []


def test_foreign_namespace_card_stays_refused_and_untaxed(fleet):
    """``<foreign_ns>:<profile>`` is ``namespace_mismatch`` and costs no capacity."""
    env = fleet
    foreign_id = _create(env, OWN_BOARD, "theirs", f"{FOREIGN_NS}:default")
    ours_id = _create(env, OWN_BOARD, "ours", "default")
    result = _tick(env, OWN_BOARD, max_in_progress_per_profile=1)

    assert [t for t, _, _ in result.spawned] == [ours_id]
    assert [(t, a, r) for t, a, r in result.skipped_namespace] == [
        (foreign_id, f"{FOREIGN_NS}:default", env.kbd.REASON_NAMESPACE_MISMATCH),
    ]
    # The refusal is durable, not a silent strand.
    with env.kbc.connect_closing(board=OWN_BOARD) as conn:
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (foreign_id,))]
    assert "dispatch_skipped" in kinds
    assert result.skipped_per_profile_capped == []


# ── the review-lane reservation mirrors the same key ────────────────────────


def test_capped_explicit_ns_review_row_does_not_reserve_the_ready_slot(
    fleet, monkeypatch, all_assignees_spawnable,
):
    """A review card whose RESOLVED profile is at cap holds no slot.

    Literal keying read ``em:reviewer`` as a profile name that was nowhere in the
    budget, so the reservation looked spawnable, held the only slot, and the ready
    lane starved for that tick.
    """
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config", lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )
    env = fleet
    # One worker running for profile ``reviewer``, written the BARE way, with the
    # review card written the EXPLICIT way: same profile, two spellings — the
    # exact shape literal keying could not see.
    _create(env, OWN_BOARD, "busy", "reviewer", running=True)
    review_id = _create_in_review(env, OWN_BOARD, "review-me", "em:reviewer")
    ready_id = _create(env, OWN_BOARD, "ready-now", "alpha")

    # Host budget 2 minus the one worker already running = 1 slot this tick.
    result = _tick(
        env, OWN_BOARD, max_in_progress=2, max_in_progress_per_profile=1,
    )
    assert [t for t, _, _ in result.spawned] == [ready_id]
    with env.kbc.connect_closing(board=OWN_BOARD) as conn:
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (review_id,)).fetchone()[0]
    assert status == "review"  # the capped review card did not take the slot either
