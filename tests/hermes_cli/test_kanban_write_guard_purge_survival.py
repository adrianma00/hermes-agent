"""Regression (#69283 root cause 2): the conftest kanban write guard must survive
the ``sys.modules`` purge that ``test_kanban_assignee_namespaces.fleet`` performs.

``fleet`` deletes every ``hermes_cli*`` / ``hermes_state*`` / ``hermes_constants``
module from ``sys.modules`` and re-imports it (lines ~378-381, to reset the
namespace memo). That REPLACES the ``hermes_cli.kanban_db_connect`` module object
that every already-imported test file holds a reference to. While the autouse
guard in ``tests/conftest.py`` armed only the object it found via ``sys.modules``,
the discarded object stayed unguarded for the rest of the session — the
order-dependent RED in ``test_kanban_write_guard.py`` (2 failed with the
namespace suite first, 25 passed reversed), i.e. the last line of defence was
down for exactly the writes that leaked 37 fixture cards into the live boards.

``_purge_like_the_namespace_suite`` is MODULE-scoped on purpose: module-scoped
fixtures are set up before the function-scoped autouse write guard, so the guard
here sees only the freshly imported object in ``sys.modules`` while this file
keeps calling through the discarded one — the post-purge state of a session,
reproduced deterministically and in either test order.

The two tests fail on the pre-fix conftest (``DID NOT RAISE`` / unmarked
``connect``) and pass once the guard is armed on every connector generation.

No real-root I/O: test 2 stubs the two I/O entry points below the guard, so a
run against the broken guard fails without opening, creating or locking
anything under ``~/.hermes`` (and ``_forbid_real_hermes_home_io`` is left
enabled as the second line of defence).
"""

from __future__ import annotations

import contextlib
import importlib
import sys

import pytest

# The PRE-purge module objects. pytest imports test modules during collection, so
# these are the objects this file calls through for the whole session — the ones
# the purge below replaces in ``sys.modules`` without invalidating our references.
from hermes_cli import kanban_db as _kb_before_purge
from hermes_cli import kanban_db_connect as _kbc_before_purge

# These tests hand the guard a path under the REAL root on purpose (that is the
# only way to prove it is armed on this module object), which is what the marker
# is for — same opt-out as ``test_kanban_write_guard.py``. The I/O entry points
# below the guard are stubbed in every test, so the opt-out can never turn into a
# real-root write even when the guard is missing.
pytestmark = pytest.mark.allow_real_home_io


@pytest.fixture(scope="module", autouse=True)
def _purge_like_the_namespace_suite():
    """Drop and re-import the kanban modules the way ``fleet`` does.

    Same prefixes, same re-import: after this runs, ``_kbc_before_purge`` /
    ``_kb_before_purge`` are the discarded generation and ``sys.modules`` holds a
    fresh one. Module scope puts the purge BEFORE the function-scoped autouse
    write guard for every test in this file.
    """
    for name in sorted(sys.modules):
        if (
            name.startswith("hermes_cli")
            or name.startswith("hermes_state")
            or name == "hermes_constants"
        ):
            del sys.modules[name]
    importlib.import_module("hermes_cli.kanban_db_connect")
    importlib.import_module("hermes_cli.kanban_db")
    assert sys.modules["hermes_cli.kanban_db_connect"] is not _kbc_before_purge, (
        "the purge did not replace the connector module object — this file no "
        "longer reproduces the post-purge state the regression is about"
    )
    yield


def _armed(module) -> bool:
    """True when ``module.connect`` carries the conftest write guard's marker."""
    return bool(getattr(getattr(module, "connect", None), "__kanban_write_guard__", False))


def test_guard_is_armed_on_the_connector_object_this_file_holds():
    """The CURRENT module object (the one callers here use) must be armed.

    ``sys.modules`` moved on to a fresh generation; the guard must cover the
    object actually called, not only the one it can look up by name. Pre-fix this
    assertion is the RED: the discarded connector is unguarded while
    ``sys.modules``'s replacement is armed.
    """
    live = sys.modules["hermes_cli.kanban_db_connect"]
    assert _armed(_kbc_before_purge), (
        "conftest's kanban write guard is NOT armed on the connector module object "
        "this file imported: the purge replaced the object the guard patched, so "
        "every write through this reference is unguarded for the rest of the "
        "session (#69283 root cause 2)"
    )
    # …and the freshly imported generation stays armed too (it always was).
    assert _armed(live), "the post-purge connector in sys.modules is unguarded"


def test_real_root_connect_through_that_object_is_refused(monkeypatch):
    """A real-root DB path called through the discarded object must raise the guard.

    ``db_path`` checks resolve nothing else, so this is the cheapest end-to-end
    proof that the guard is live on that object. Both I/O entry points below the
    guard are stubbed: with the guard armed the ``RuntimeError`` fires first;
    with the guard down (pre-fix) the stub fires before SQLite or the init lock
    is touched, so the test is evidence-only either way and never writes.
    """
    reached: list[str] = []

    def _no_sqlite(*_args, **_kwargs):
        reached.append("sqlite")
        raise AssertionError("connect() reached sqlite with the write guard down")

    @contextlib.contextmanager
    def _no_init_lock(*_args, **_kwargs):
        reached.append("init_lock")
        raise AssertionError("connect() took the init lock with the write guard down")

    monkeypatch.setattr(_kbc_before_purge, "_sqlite_connect", _no_sqlite)
    monkeypatch.setattr(_kbc_before_purge, "_cross_process_init_lock", _no_init_lock)

    import tests.conftest as _conftest

    probe = _conftest._REAL_KANBAN_ROOT / "kanban_write_guard_purge_probe.db"
    with pytest.raises(RuntimeError, match="kanban_write_guard"):
        _kbc_before_purge.connect(probe)
    assert reached == [], f"the guard let connect() through: {reached}"


def test_guard_resolves_the_board_path_through_the_callers_kanban_db(monkeypatch):
    """No ``db_path``: the guard must resolve via the CALLER's ``kanban_db``.

    The ``kanban_home``-is-the-real-root probe monkeypatches the object this file
    imported; the guard has to consult that same generation (it is reached via
    the connector's own ``_kb`` binding) or it would resolve a sandboxed path and
    wave the write through. The real file's failure mode is exactly this object
    mismatch, one level down.
    """
    import tests.conftest as _conftest

    reached: list[str] = []

    def _no_sqlite(*_args, **_kwargs):
        reached.append("sqlite")
        raise AssertionError("connect() reached sqlite with the write guard down")

    @contextlib.contextmanager
    def _no_init_lock(*_args, **_kwargs):
        reached.append("init_lock")
        raise AssertionError("connect() took the init lock with the write guard down")

    probe = _conftest._REAL_KANBAN_ROOT / "kanban_write_guard_purge_probe.db"
    monkeypatch.setattr(_kbc_before_purge, "_sqlite_connect", _no_sqlite)
    monkeypatch.setattr(_kbc_before_purge, "_cross_process_init_lock", _no_init_lock)
    # The two resolution hooks the guard must go through: this object, not the
    # post-purge one whose path would land in the sandbox and pass.
    monkeypatch.setattr(_kb_before_purge, "kanban_home", lambda: _conftest._REAL_KANBAN_ROOT)
    monkeypatch.setattr(_kb_before_purge, "kanban_db_path", lambda board=None: probe)
    with pytest.raises(RuntimeError, match="kanban_write_guard"):
        _kbc_before_purge.connect()
    assert reached == [], f"the guard let connect() through: {reached}"
