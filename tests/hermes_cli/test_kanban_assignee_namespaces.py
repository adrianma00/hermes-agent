"""Assignee namespaces: whose ``default`` is it? (proven shared-board race)

``default`` is INSTALL-RELATIVE — it means "the top-level agent of the install
doing the claiming", and ``profiles.profile_exists("default")`` answers True in
EVERY install. So on a board two installs sweep, a ``default`` card was spawnable
in both and ran in whichever dispatcher ticked first (probe ``t_45f8aaac`` ran
here instead of in the owning install). Same defect family as the
``HERMES_KANBAN_DB`` pin work: a name resolved without the ownership context it
depends on.

The fix puts the disambiguation in the assignee itself. A profile's identity is
the PAIR ``(namespace, name)``, never the bare name alone:

* ``ns:name`` — canonical, explicit; claimed only by the install owning ``ns``.
* bare name — resolves within the BOARD's declared namespace, so bare
  ``default`` on a board whose namespace is the other install means THAT
  install's top-level agent, and this one refuses it.
* bare name on a board that declares no namespace — unchanged name
  partitioning (the compatibility guarantee), EXCEPT an install-relative name
  (``default``), which is ambiguous by construction and therefore refused.

Every refusal is VISIBLE: a ``skipped_namespace`` bucket on ``DispatchResult``,
a ``dispatch_skipped`` task event carrying a remedy, and a warning on the tick.
A silent skip is the bug class this feature exists to prevent, so the regression
guard here asserts the durable record, not just the boolean.

Test mechanics: one temp ``HERMES_HOME`` per test holds the board(s) and the
profile directories; "which install is ticking" is chosen by
``HERMES_KANBAN_NAMESPACE``, exactly as deploy sets it via ``kanban.namespace``.
Profile *existence* is necessarily shared in a single-home test, so cases that
turn on "this namespace has no such profile" use a profile name that is absent
from the fixture's profile list.

Profiles are constructed the way the runtime recognises them — a
``profiles/<name>/`` directory carrying an identity marker
(:data:`_PROFILE_IDENTITY_FILE`), not a bare ``mkdir``. ``profiles.profile_exists``
delegates to ``hermes_constants.named_profile_is_live``, which requires one of
``_PROFILE_IDENTITY_MARKERS``; a bare directory answers False, so the dispatcher
refused every assignee resolving to a named profile with
``profile_missing_in_namespace``: three tests stayed red against a fixture whose
disk layout looked correct. :func:`_assert_fixture_profiles_constructible` fails
loud on that now.

Isolation, and why it is asserted rather than assumed: the same file, run from a
Hermes-launched shell, once wrote 37 of its fixture cards into the PRODUCTION
board DBs (2 default / 11 em-admin / 24 shared, plus a live ``board.json``
rewrite) and the live dispatcher spawned real workers for them. Three
cooperating faults, all fixed here and all re-guarded loudly:

1. ``tempfile.mkdtemp()`` honours ``TMPDIR``, and a Hermes-launched pytest
   arrives with ``TMPDIR=<home>/cache/scratch`` — INSIDE the platform default
   root. ``hermes_constants.get_default_hermes_root()`` reads a ``HERMES_HOME``
   under that root as "normal or profile mode" and returns the NATIVE root, so
   ``kanban_db.kanban_home()`` collapsed onto the LIVE root. The fixture home now
   comes from :func:`_fixture_base_dir` (outside the default root) and is also
   pinned via ``HERMES_KANBAN_HOME``, which ``kanban_home()`` prefers.
2. The fixture inherited the dispatcher's ``HERMES_KANBAN_DB`` /
   ``HERMES_KANBAN_BOARD`` pins. ``_pin_answers_for()`` answers a request for the
   pinned slug with the PINNED file, so the em-admin cases wrote to the live
   em-admin DB even from a perfectly isolated home. All pins are stripped now.
3. ``fleet`` purges every ``hermes_cli*`` module and re-imports it, which threw
   away the object ``tests/conftest.py::_kanban_write_guard`` had patched — the
   guard was silently dead for every write the fixture made. The fixture re-arms
   a guard on the freshly imported connector (an allow-list on its own home).

:func:`_assert_fixture_home_isolated` is the fail-loud tripwire the next
regression needs: the previous failures were silent.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.kanban_db_dispatch import (
    REASON_ASSIGNEE_INVALID,
    REASON_NAMESPACE_MISMATCH,
    REASON_NAMESPACE_UNDECLARED,
    REASON_NAMESPACE_UNRESOLVED,
    REASON_PROFILE_MISSING,
    assignee_profile,
    install_namespaces,
    resolve_assignee,
    split_assignee,
)


def _fake_spawn(*args, **kwargs):
    return 12345


# ── Fixture isolation plumbing (see the module docstring) ───────────────────

# Pins the dispatcher injects into every worker and descendant. They identify
# THAT process's own board, so a fixture that keeps them writes its own board's
# cards into the LIVE file (`_pin_answers_for`), however isolated its home is.
_INHERITED_KANBAN_PINS = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT",
)

# A directory under ``profiles/`` is a profile only when something identifies it
# as one: ``hermes_constants.named_profile_is_live`` requires a marker from
# ``hermes_constants._PROFILE_IDENTITY_MARKERS`` (``config.yaml``, ``.env``,
# ``SOUL.md``, ``profile.yaml``, ``auth.json``, ``state.db``). ``config.yaml`` is
# what ``hermes profile create`` writes first, so the fixture writes that.
# A bare directory is NOT a live profile: ``profiles.profile_exists`` answers
# False for it, which is how three tests in this file stayed red — every assignee
# resolving to a named profile was refused with ``profile_missing_in_namespace``
# even though the fixture's home was perfectly isolated.
_PROFILE_IDENTITY_FILE = "config.yaml"
_PROFILE_IDENTITY_BODY = (
    "# Written by tests/hermes_cli/test_kanban_assignee_namespaces.py.\n"
    "# The marker itself is the point: profiles/<name>/ counts as a live profile\n"
    "# only when it carries one of hermes_constants._PROFILE_IDENTITY_MARKERS,\n"
    "# which is what profiles.profile_exists() (and so the dispatcher's assignee\n"
    "# check) resolves against. The mapping is empty on purpose — nothing here\n"
    "# should change what the code under test reads.\n"
    "{}\n"
)

# One per-process base for the fixture homes. Cleaned up at interpreter exit so
# a run stops leaving ``kanban_assignee_ns_test_*`` homes behind (the pre-fix
# shape left 36 under the coder profile's scratch dir).
_FIXTURE_BASE: Path | None = None


def _platform_default_root() -> Path:
    """The platform default Hermes root (``~/.hermes``) as the code under test sees it.

    Under pytest ``tests/conftest.py::_hermetic_environment`` points this at a
    per-test tempdir, so the guard below is hermetic; in production it is the
    operator's real ``~/.hermes``.
    """
    from hermes_constants import _get_platform_default_hermes_home
    return Path(_get_platform_default_hermes_home()).expanduser()


def _is_under(path, root) -> bool:
    """True when *path* IS *root* or lives below it (both sides resolved)."""
    try:
        resolved, base = Path(path).expanduser().resolve(), Path(root).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == base or base in resolved.parents


def _fixture_base_dir() -> Path:
    """A writable base for fixture homes, provably OUTSIDE the default root.

    ``tempfile.mkdtemp()`` honours ``TMPDIR``, and a Hermes-launched pytest
    arrives with ``TMPDIR=<home>/cache/scratch``. A ``HERMES_HOME`` under the
    platform default root is read by ``hermes_constants.get_default_hermes_root()``
    as "normal or profile mode" — it returns the NATIVE root — so
    ``kanban_db.kanban_home()`` collapsed onto the LIVE root and these fixtures
    were written into the production boards. Pick the base explicitly instead:
    never the inherited TMPDIR, never inside the default root.
    """
    global _FIXTURE_BASE
    if _FIXTURE_BASE is not None and _FIXTURE_BASE.is_dir():
        return _FIXTURE_BASE
    native = _platform_default_root()
    for candidate in (
        os.environ.get("HERMES_TEST_KANBAN_HOME_BASE", "").strip(),
        "/var/tmp",
        "/tmp",
        tempfile.gettempdir(),
    ):
        if not candidate:
            continue
        parent = Path(candidate).expanduser()
        if _is_under(parent, native):
            continue  # inside the default root: the exact vector being fixed
        try:
            parent.mkdir(parents=True, exist_ok=True)
            base = Path(tempfile.mkdtemp(prefix="kanban_assignee_ns_base_", dir=str(parent)))
        except OSError:
            continue
        atexit.register(shutil.rmtree, str(base), True)
        _FIXTURE_BASE = base
        return base
    raise RuntimeError(
        "kanban test isolation: no writable fixture base outside the platform "
        f"default root ({native}); point HERMES_TEST_KANBAN_HOME_BASE at one"
    )


def _assert_fixture_home_isolated(home) -> None:
    """Fail LOUD when the fixture's home is not actually isolated.

    The pre-fix failures were silent: the fixture built a "fresh" home, the board
    paths collapsed onto the live root, and the run went green while writing 37
    cards into production. Everything a fixture writes is derived from
    ``kanban_db.kanban_home()``, so check the resolution, not the intent:

    * the home must not be inside the platform default root, and
      ``get_default_hermes_root()`` must not collapse it onto that root;
    * the resolved kanban home must still be inside the fixture home;
    * ``kanban_db_path("default")`` must not be the live ``<root>/kanban.db``.
    """
    native = _platform_default_root()
    resolved = Path(home).expanduser()
    if _is_under(resolved, native):
        raise AssertionError(
            f"kanban test isolation: fixture home {resolved} is inside the "
            f"platform default root {native}. A HERMES_HOME there resolves to the "
            f"NATIVE root, so this fixture would write its cards into the LIVE "
            f"boards — the usual cause is a home built under TMPDIR= "
            f"<home>/cache/scratch. Build it with _fixture_base_dir() instead."
        )
    from hermes_constants import get_default_hermes_root
    collapsed = Path(get_default_hermes_root(home=str(resolved))).expanduser()
    if collapsed.resolve() != resolved.resolve():
        raise AssertionError(
            f"kanban test isolation: hermes_constants.get_default_hermes_root() "
            f"collapses fixture home {resolved} onto {collapsed}; kanban paths would "
            f"resolve there instead of inside the fixture home."
        )
    from hermes_cli.kanban_db import kanban_db_path, kanban_home
    kh = Path(kanban_home()).expanduser()
    if not _is_under(kh, resolved):
        raise AssertionError(
            f"kanban test isolation: kanban_db.kanban_home() resolves to {kh}, "
            f"outside the fixture home {resolved} (inherited pin?)."
        )
    default_db = Path(kanban_db_path("default")).expanduser()
    if _is_under(default_db, native):
        raise AssertionError(
            f"kanban test isolation: kanban_db_path('default') resolves to "
            f"{default_db}, under the platform default root {native} — that is the "
            f"LIVE home board. See tests/hermes_cli/test_kanban_assignee_namespaces.py."
        )


def _assert_fixture_profiles_constructible(home, names) -> None:
    """Fail LOUD when the fixture's profiles are not the ones the runtime sees.

    ``dispatch_once`` asks ``profiles.profile_exists(<assignee>)``, and that
    resolves through ``hermes_constants.get_default_hermes_root()`` →
    ``<root>/profiles/<name>`` live — nothing the fixture passes in. So both
    halves have to hold, and each was previously checked only by eye:

    * the profiles root this process will read must be inside the fixture home
      (the platform default root's own ``profiles/`` means the LIVE install's
      profile set is answering);
    * every profile the fixture declares must satisfy ``profile_exists`` — a bare
      directory does not, because ``named_profile_is_live`` requires an identity
      marker (:data:`_PROFILE_IDENTITY_FILE`).

    A fault in either half shows up as a ``profile_missing_in_namespace`` refusal
    that reads like a namespace-logic bug: exactly how the three 2026-09-30 reds
    in this file were first misdiagnosed.
    """
    from hermes_cli import profiles as profiles_mod

    home_path = Path(home).expanduser().resolve()
    native = _platform_default_root()
    root = Path(profiles_mod._get_profiles_root()).expanduser()
    if not _is_under(root, home_path) or _is_under(root, native):
        raise AssertionError(
            f"kanban test isolation: the profiles root this process reads is {root}, "
            f"which is not inside the fixture home {home_path} (platform default "
            f"root: {native}). profiles.profile_exists() would answer for the live "
            f"install's profile set, not the fixture's — the dispatcher would then "
            f"refuse (or claim) assignees for the wrong reason."
        )
    wrong_home = [n for n in names if not _is_under(profiles_mod.get_profile_dir(n), home_path)]
    if wrong_home:
        raise AssertionError(
            f"kanban test isolation: fixture profiles {wrong_home} do not resolve "
            f"under the fixture home {home_path}."
        )
    missing = [n for n in names if not profiles_mod.profile_exists(n)]
    if missing:
        raise AssertionError(
            f"kanban test fixture: profiles {missing} do not satisfy "
            f"profiles.profile_exists() under {root}. A bare directory is not a live "
            f"profile — hermes_constants.named_profile_is_live requires an identity "
            f"marker ({_PROFILE_IDENTITY_FILE}); the dispatcher refuses every "
            f"assignee resolving to them with profile_missing_in_namespace."
        )


def _assert_pins_stripped(home: Path) -> None:
    """The inherited dispatcher pins must be gone; only this fixture's may remain.

    ``HERMES_KANBAN_HOME`` is on the strip list AND set by the fixture (to the
    fixture home) — the point is that no INHERITED value survived.
    """
    assert os.environ.get("HERMES_HOME") == str(home), (
        f"HERMES_HOME is {os.environ.get('HERMES_HOME')!r}, not the fixture home {home}"
    )
    for var in _INHERITED_KANBAN_PINS:
        value = os.environ.get(var)
        if var == "HERMES_KANBAN_HOME":
            assert value == str(home), f"{var} kept an inherited value: {value!r}"
        else:
            assert value is None, f"{var} leaked into the fixture env: {value!r}"


def _arm_fixture_connect_guard(monkeypatch, kbc, kb, home: Path) -> None:
    """Re-arm a write guard on the FRESHLY imported connector.

    ``tests/conftest.py::_kanban_write_guard`` patches ``kanban_db_connect.connect``
    on the module object present when it runs. ``fleet`` purges every
    ``hermes_cli*`` module and re-imports it, so that object is dead by the time
    the test writes and the guard never sees it (fault 3 of the 2026-09-30 leak).
    Patch the live object — and use an allow-list on THIS fixture's home instead
    of conftest's deny-list: a connection anywhere else is a bug in this fixture,
    not merely a live-root write, and an allow-list cannot go stale.
    """
    original = kbc.connect

    def _connect_in_fixture_home(db_path=None, *args, **kwargs):
        target = db_path
        if target is None:
            target = kb.kanban_db_path(board=kwargs.get("board"))
        if not _is_under(target, home):
            raise AssertionError(
                f"kanban test isolation: refusing to open {Path(target).expanduser()}, "
                f"outside the fixture home {Path(home).resolve()} — this fixture would "
                f"have written to a board that is not its own (inherited "
                f"HERMES_KANBAN_DB pin, or an unisolated home)."
            )
        return original(db_path, *args, **kwargs)

    monkeypatch.setattr(kbc, "connect", _connect_in_fixture_home)


@pytest.fixture()
def fleet(monkeypatch):
    """Build a fresh install home and hand back the imported modules.

    ``fleet(profiles=(...), boards={"slug": namespace-or-None})``. The returned
    namespace has ``.as_install(ns)`` to choose which install's dispatcher the
    next tick belongs to.

    The home is created OUTSIDE the platform default root, the inherited
    dispatcher pins are stripped, and both are asserted (see
    :func:`_assert_fixture_home_isolated`) — a fixture that stops isolating must
    fail, not write into the live boards again.
    """
    def build(*, profiles=("default", "admin"), boards=(("shared", None),)):
        home = Path(
            tempfile.mkdtemp(prefix="kanban_assignee_ns_test_", dir=str(_fixture_base_dir()))
        )
        for prof in profiles:
            profile_dir = home / "profiles" / prof
            profile_dir.mkdir(parents=True, exist_ok=True)
            # An identity marker, not just a directory: profile_exists() is
            # routed through named_profile_is_live() and a bare mkdir answers
            # False (see _PROFILE_IDENTITY_FILE).
            (profile_dir / _PROFILE_IDENTITY_FILE).write_text(
                _PROFILE_IDENTITY_BODY, encoding="utf-8"
            )
        # Strip the dispatcher's pins BEFORE anything resolves a board path:
        # inherited, they make the caller's own board (HERMES_KANBAN_BOARD) answer
        # with the LIVE pinned file no matter how isolated the home is.
        for var in _INHERITED_KANBAN_PINS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HERMES_HOME", str(home))
        # kanban_home() prefers this var over get_default_hermes_root(), so the
        # board paths stay inside the fixture home even if a later HERMES_HOME
        # change would collapse onto the native root.
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_NAMESPACE", "em")
        _assert_pins_stripped(home)
        for mod in list(sys.modules):
            if (mod.startswith("hermes_cli") or mod.startswith("hermes_state")
                    or mod == "hermes_constants"):
                del sys.modules[mod]
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        from hermes_cli import kanban_db_dispatch as kbd
        # The purge above re-imported the modules conftest's guard had patched:
        # re-arm on the live object before the first write.
        _arm_fixture_connect_guard(monkeypatch, kbc, kb, home)
        # Fail loud BEFORE any board is created, against the freshly imported
        # modules the writes will actually go through.
        _assert_fixture_home_isolated(home)
        # …and equally loud about the profile set the dispatcher will consult:
        # an isolated home whose profiles are not resolvable as live profiles is
        # what kept three tests in this file red against the LIVE install's
        # profile names while looking correct on disk.
        _assert_fixture_profiles_constructible(home, profiles)
        for slug, namespace in boards.items():
            kb.create_board(slug=slug, name=slug, namespace=namespace)
        # The install namespace is resolved once per process and memoised, so a
        # fresh module (above) is a fresh startup. Clearing explicitly keeps a
        # test that runs after an in-process env swap honest either way.
        kbd._reset_namespace_cache()
        env = SimpleNamespace(
            home=str(home), kb=kb, kbc=kbc, kbd=kbd, boards=dict(boards),
            monkeypatch=monkeypatch,
        )

        def as_install(ns):
            """Tick as though THIS install's dispatcher were the one running.

            ``HERMES_KANBAN_NAMESPACE`` is part of the namespace memo key, so the
            swap is picked up anyway; the documented reset hook is called too, so
            the tests exercise it (and so a config-derived namespace, which is
            only read once per process, would be re-read).
            """
            monkeypatch.setenv("HERMES_KANBAN_NAMESPACE", ns)
            kbd._reset_namespace_cache()

        env.as_install = as_install
        return env

    return build


def _tick(env, board, **kwargs):
    with env.kbc.connect_closing(board=board) as conn:
        return env.kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, board=board, **kwargs,
        )


def _create(env, board, *, title, assignee):
    with env.kbc.connect_closing(board=board) as conn:
        return env.kb.create_task(conn, title=title, assignee=assignee)


def _task(env, board, task_id):
    with env.kbc.connect_closing(board=board) as conn:
        return conn.execute(
            "SELECT status, assignee, claim_lock FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()


def _events(env, board, task_id, kind):
    with env.kbc.connect_closing(board=board) as conn:
        return [
            json.loads(row["payload"] or "{}")
            for row in conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
                "ORDER BY id", (task_id, kind),
            )
        ]


def _run_profiles(env, board, task_id):
    with env.kbc.connect_closing(board=board) as conn:
        return [
            row["profile"] for row in conn.execute(
                "SELECT profile FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,),
            )
        ]


def _skip_reasons(result):
    return {tid: reason for (tid, _who, reason) in result.skipped_namespace}


# ---------------------------------------------------------------------------
# The race itself
# ---------------------------------------------------------------------------


def test_bare_default_claimed_only_by_the_board_namespace_owner(fleet):
    """A bare ``default`` on a board whose namespace is the other install is
    claimed by THAT install and refused here — with a durable record."""
    env = fleet(boards={"shared": "yummi"})
    task_id = _create(env, "shared", title="her top-level", assignee="default")

    env.as_install("em")
    res = _tick(env, "shared")
    assert res.spawned == []
    assert _skip_reasons(res) == {task_id: REASON_NAMESPACE_MISMATCH}
    assert task_id not in res.skipped_nonspawnable
    assert _task(env, "shared", task_id)["status"] == "ready"
    # Visible, not silent: a durable event the dashboard can show.
    skips = _events(env, "shared", task_id, "dispatch_skipped")
    assert [s["reason"] for s in skips] == [REASON_NAMESPACE_MISMATCH]
    assert skips[0]["board_namespace"] == "yummi"
    assert skips[0]["install_namespace"] == "em"
    assert "yummi" in skips[0]["remedy"]

    # Her dispatcher (same DB, her namespace) claims it, as plain ``default``.
    env.as_install("yummi")
    res = _tick(env, "shared")
    assert [s[0] for s in res.spawned] == [task_id]
    assert res.skipped_namespace == []
    assert _task(env, "shared", task_id)["status"] == "running"
    # The run row names the profile that ran, not the routing string.
    assert _run_profiles(env, "shared", task_id) == ["default"]


def test_explicit_namespaced_default_claimed_only_by_matching_install(fleet):
    """``yummi:default`` is the canonical form: it works on ANY board, declared
    or not, and only the owning install may claim it."""
    env = fleet(boards={"shared": None})
    task_id = _create(env, "shared", title="explicit", assignee="yummi:default")

    env.as_install("em")
    res = _tick(env, "shared")
    assert res.spawned == []
    assert _skip_reasons(res) == {task_id: REASON_NAMESPACE_MISMATCH}
    assert _events(env, "shared", task_id, "dispatch_skipped")[0]["reason"] == (
        REASON_NAMESPACE_MISMATCH
    )

    env.as_install("yummi")
    res = _tick(env, "shared")
    assert [s[0] for s in res.spawned] == [task_id]
    # Namespace stripped for the spawn: the profile is ``default``.
    assert _run_profiles(env, "shared", task_id) == ["default"]


def test_namespace_token_parsed_case_insensitively(fleet):
    """The human form ``Yummi:default`` means ``yummi:default``."""
    assert split_assignee("Yummi:default") == ("yummi", "default")
    assert split_assignee("  EM : admin ") == ("em", "admin")
    assert split_assignee("default") == (None, "default")
    assert split_assignee(None) == (None, "")
    assert assignee_profile("Yummi:default") == "default"

    env = fleet(boards={"shared": None})
    task_id = _create(env, "shared", title="case", assignee="Yummi:default")
    env.as_install("yummi")
    assert [s[0] for s in _tick(env, "shared").spawned] == [task_id]


# ---------------------------------------------------------------------------
# No regression on our own boards
# ---------------------------------------------------------------------------


def test_bare_default_on_our_own_board_unchanged(fleet):
    """``default`` on a board this install owns keeps working exactly as before.
    A large amount of live work depends on it."""
    env = fleet(boards={"em-admin": "em"})
    task_id = _create(env, "em-admin", title="ours", assignee="default")

    env.as_install("em")
    res = _tick(env, "em-admin")
    assert [s[0] for s in res.spawned] == [task_id]
    assert res.skipped_namespace == []
    assert _run_profiles(env, "em-admin", task_id) == ["default"]
    # Another install sweeping the same board does not take it.
    other = _create(env, "em-admin", title="ours too", assignee="default")
    env.as_install("yummi")
    res = _tick(env, "em-admin")
    assert other not in [s[0] for s in res.spawned]
    assert _skip_reasons(res)[other] == REASON_NAMESPACE_MISMATCH


def test_default_board_needs_no_declaration(fleet):
    """The ``default`` board's DB is ``<this install root>/kanban.db`` by
    construction, so no other install can reach it and no declaration is needed
    — that is a property of the slug→path mapping, not a symlink inference."""
    env = fleet(boards={"default": None})
    # …and that root is the FIXTURE's home, not the operator's: this is the card
    # that used to be written into the live ``<root>/kanban.db`` (and which made
    # the live dispatcher spawn a worker for it).
    assert Path(env.kb.kanban_db_path("default")).resolve() == (
        Path(env.home).resolve() / "kanban.db"
    )
    assert not _is_under(Path(env.home), _platform_default_root())
    task_id = _create(env, "default", title="home board", assignee="default")
    for ns in ("em", "yummi", "whoever"):
        env.as_install(ns)
        res = _tick(env, "default", dry_run=True)
        assert task_id in [s[0] for s in res.spawned], ns
        assert res.skipped_namespace == [], ns


# ---------------------------------------------------------------------------
# Fixture isolation — the 2026-09-30 live-board leak, as a regression guard
# ---------------------------------------------------------------------------


def test_fixture_stays_isolated_when_tmpdir_points_inside_the_native_root(
    fleet, monkeypatch
):
    """A Hermes-launched pytest has ``TMPDIR=<home>/cache/scratch``, i.e. INSIDE
    the platform default root. The old home construction — plain
    ``tempfile.mkdtemp()``, which honours TMPDIR — therefore landed inside that
    root, ``kanban_home()`` collapsed onto it, and the fixture wrote 37 cards into
    the production boards (2 default / 11 em-admin / 24 shared).

    The native root here is the per-test one ``_hermetic_environment`` installs
    (a tempdir), never the operator's real ``~/.hermes`` — the mechanism is the
    same, and the test stays hermetic. Both halves are asserted: the OLD
    construction is rejected loudly by the guard the fixture runs, the shipped
    one stays outside.
    """
    native = _platform_default_root()
    if native.resolve() == (Path.home() / ".hermes").resolve():
        pytest.skip("conftest did not isolate the platform default root; refusing to write in it")
    scratch = native / "cache" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(tempfile, "tempdir", None)

    # The old construction, verbatim: mkdtemp() honours TMPDIR.
    legacy_home = Path(tempfile.mkdtemp(prefix="kanban_assignee_ns_test_"))
    assert _is_under(legacy_home, native), "TMPDIR was not honoured by mkdtemp"
    with pytest.raises(AssertionError, match="platform default root"):
        _assert_fixture_home_isolated(legacy_home)

    # The shipped fixture picks a base outside that root instead.
    env = fleet(boards={"shared": None})
    assert not _is_under(env.home, native)
    assert Path(env.kb.kanban_home()).resolve() == Path(env.home).resolve()
    assert _is_under(env.kb.kanban_db_path("default"), env.home)


def test_fixture_strips_inherited_dispatcher_pins(fleet, monkeypatch, tmp_path):
    """A worker gets ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` for ITS board.
    Inherited by this fixture, ``_pin_answers_for()`` answered the em-admin cases
    with the LIVE pinned file even from a perfectly isolated home — the second
    way the 2026-09-30 run wrote into production."""
    pinned_db = tmp_path / "live" / "kanban" / "boards" / "em-admin" / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned_db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "em-admin")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path / "live" / "kanban" / "workspaces"))

    env = fleet(boards={"em-admin": "em"})
    _assert_pins_stripped(Path(env.home))

    _create(env, "em-admin", title="ours", assignee="default")
    # The card went to the fixture's own board file; the pinned file was untouched.
    assert not pinned_db.exists()
    assert _is_under(env.kb.kanban_db_path("em-admin"), env.home)


def test_connect_guard_rejects_a_connection_outside_the_fixture_home(fleet, tmp_path):
    """The guard re-armed after the module purge (conftest's own guard patched the
    object the purge then threw away — fault 3) must refuse a connection that is
    not the fixture's, rather than letting the write land somewhere live."""
    env = fleet(boards={"shared": None})
    with pytest.raises(AssertionError, match="outside the fixture home"):
        with env.kbc.connect_closing(db_path=tmp_path / "elsewhere" / "kanban.db"):
            pass  # pragma: no cover - the connect above must raise


def test_fixture_profiles_are_live_the_way_the_runtime_sees_them(fleet):
    """The fixture's profiles must satisfy the SAME predicate the dispatcher asks,
    resolved the same way — and the guard must fail loud on the pre-fix shape.

    ``profiles.profile_exists('admin')`` answered False while the fixture built
    bare ``profiles/admin`` directories, because ``named_profile_is_live`` wants
    an identity marker. The symptom was not "admin is missing from the fixture"
    but ``profile_missing_in_namespace`` refusals that read like namespace-logic
    bugs, so the guard asserts both halves: the resolved profiles root is the
    fixture's, and the declared names are live there.
    """
    from hermes_cli import profiles as profiles_mod

    env = fleet(profiles=("default", "admin"), boards={"shared": None})
    root = Path(profiles_mod._get_profiles_root())
    assert _is_under(root, env.home), f"profiles root {root} is not the fixture's"
    assert profiles_mod.profile_exists("admin")
    marker = Path(env.home) / "profiles" / "admin" / _PROFILE_IDENTITY_FILE
    assert marker.is_file(), f"no identity marker at {marker}"

    # The pre-fix construction, verbatim, must now be rejected loudly.
    bare = Path(env.home) / "profiles" / "bare-profile"
    bare.mkdir()
    assert profiles_mod.profile_exists("bare-profile") is False, (
        "a bare directory must not count as a live profile, or this guard proves nothing"
    )
    with pytest.raises(AssertionError, match="do not satisfy"):
        _assert_fixture_profiles_constructible(env.home, ["default", "bare-profile"])


# ---------------------------------------------------------------------------
# No new silent skip — the regression guard
# ---------------------------------------------------------------------------


def test_bare_default_on_undeclared_board_refused_VISIBLY(fleet):
    """An install-relative bare name on a board that declares no namespace is
    ambiguous by construction: refused, never raced — and the refusal leaves a
    trace (log line, ``skipped_namespace`` bucket, one durable event)."""
    env = fleet(boards={"shared": None})
    task_id = _create(env, "shared", title="ambiguous", assignee="default")

    for ns in ("em", "yummi"):
        env.as_install(ns)
        res = _tick(env, "shared")
        assert res.spawned == [], ns
        assert _skip_reasons(res) == {task_id: REASON_NAMESPACE_UNDECLARED}, ns
        # NEVER the invisible bucket: that is the bug class being fixed.
        assert task_id not in res.skipped_nonspawnable, ns
        assert _task(env, "shared", task_id)["status"] == "ready", ns

    skips = _events(env, "shared", task_id, "dispatch_skipped")
    assert [s["reason"] for s in skips] == [REASON_NAMESPACE_UNDECLARED]
    assert skips[0]["board"] == "shared"
    assert "set-namespace" in skips[0]["remedy"]
    # One event per (task, assignee, reason) — not one per tick.
    env.as_install("em")
    _tick(env, "shared")
    assert len(_events(env, "shared", task_id, "dispatch_skipped")) == 1


def test_unresolvable_install_namespace_refused_visibly(fleet):
    """This install cannot prove which namespace it is: refuse rather than
    guess, and say so."""
    env = fleet(boards={"shared": "yummi"})
    task_id = _create(env, "shared", title="who am i", assignee="default")
    env.monkeypatch.setenv("HERMES_KANBAN_NAMESPACE", "not a token!")
    assert install_namespaces() == (None, frozenset())

    res = _tick(env, "shared")
    assert res.spawned == []
    assert _skip_reasons(res) == {task_id: REASON_NAMESPACE_UNRESOLVED}
    assert _events(env, "shared", task_id, "dispatch_skipped")[0]["reason"] == (
        REASON_NAMESPACE_UNRESOLVED
    )


def test_profile_missing_in_namespace_refused_visibly(fleet):
    """The name resolved through a namespace and this install has no such
    profile — operator-actionable (place it explicitly), so it must be visible
    rather than joining the invisible control-plane bucket."""
    env = fleet(profiles=("default",), boards={"em-admin": "em"})
    task_id = _create(env, "em-admin", title="ghost", assignee="quantconnect-dev-test")

    env.as_install("em")
    res = _tick(env, "em-admin")
    assert res.spawned == []
    assert _skip_reasons(res) == {task_id: REASON_PROFILE_MISSING}
    assert task_id not in res.skipped_nonspawnable
    event = _events(env, "em-admin", task_id, "dispatch_skipped")[0]
    assert event["reason"] == REASON_PROFILE_MISSING
    assert "quantconnect-dev-test" in event["remedy"]


def test_empty_assignee_is_refused_visibly(fleet):
    """``ns:`` with no profile behind it is a routing mistake, not a lane."""
    env = fleet(boards={"shared": None})
    task_id = _create(env, "shared", title="no profile", assignee="em:")
    env.as_install("em")
    res = _tick(env, "shared")
    assert _skip_reasons(res) == {task_id: REASON_ASSIGNEE_INVALID}


# ---------------------------------------------------------------------------
# Compatibility: bare unique names keep partitioning by name
# ---------------------------------------------------------------------------


def test_bare_unique_names_unchanged_on_undeclared_board(fleet):
    """No namespace declared ⇒ behaviour identical to today: the profile set
    decides, and bare unique names are not namespace-gated at all (that path is
    only reachable once a board declares a namespace). In one home both installs
    see the same profile directories, so this asserts the gate does not fire in
    either direction rather than partition ownership — ownership by name is what
    an undeclared board still relies on."""
    env = fleet(profiles=("default", "admin"), boards={"shared": None})
    ours = _create(env, "shared", title="ours", assignee="admin")
    # A name this install does not have takes today's immutable path: the
    # non-spawnable bucket, unchanged — deliberately NOT a namespace refusal,
    # because the board declares no namespace for the name to be foreign to.
    absent = _create(env, "shared", title="absent", assignee="her-bot")

    env.as_install("em")
    res = _tick(env, "shared")
    assert [s[0] for s in res.spawned] == [ours]
    assert res.skipped_namespace == []
    assert absent in res.skipped_nonspawnable

    env.as_install("yummi")
    other = _create(env, "shared", title="hers", assignee="admin")
    res = _tick(env, "shared")
    assert [s[0] for s in res.spawned] == [other]
    assert res.skipped_namespace == []


def test_bare_name_resolves_through_the_board_namespace(fleet):
    """On a board that declares a namespace, EVERY bare name resolves in it —
    not just install-relative ones (two identically named profiles in two
    namespaces would otherwise collide exactly like ``default`` did)."""
    env = fleet(profiles=("default", "admin"), boards={"shared": "yummi"})
    task_id = _create(env, "shared", title="bare", assignee="admin")

    env.as_install("em")
    res = _tick(env, "shared")
    assert res.spawned == []
    assert _skip_reasons(res) == {task_id: REASON_NAMESPACE_MISMATCH}

    env.as_install("yummi")
    assert [s[0] for s in _tick(env, "shared").spawned] == [task_id]


# ---------------------------------------------------------------------------
# Explicit cross-namespace placement
# ---------------------------------------------------------------------------


def test_explicit_override_places_work_across_namespaces(fleet):
    """``em:admin`` on her board runs here. The card keeps the assignee it was
    given; only the run row records the resolved profile."""
    env = fleet(profiles=("default", "admin"), boards={"shared": "yummi"})
    task_id = _create(env, "shared", title="borrowed", assignee="em:admin")

    env.as_install("em")
    res = _tick(env, "shared")
    assert [s[0] for s in res.spawned] == [task_id]
    assert _run_profiles(env, "shared", task_id) == ["admin"]
    assert _task(env, "shared", task_id)["assignee"] == "em:admin"


# ---------------------------------------------------------------------------
# Fallbacks and telemetry
# ---------------------------------------------------------------------------


def test_default_assignee_fallback_not_written_onto_a_foreign_board(fleet):
    """``kanban.default_assignee`` must not stamp an unresolvable
    install-relative name onto another install's card — that would mutate their
    board and claim a routing decision this install has no standing to make."""
    env = fleet(boards={"shared": "yummi"})
    with env.kbc.connect_closing(board="shared") as conn:
        task_id = env.kb.create_task(conn, title="unassigned", assignee=None)

    env.as_install("em")
    res = _tick(env, "shared", default_assignee="default")
    assert res.auto_assigned_default == []
    assert _skip_reasons(res) == {task_id: REASON_NAMESPACE_MISMATCH}
    assert _task(env, "shared", task_id)["assignee"] is None
    assert _events(env, "shared", task_id, "assigned") == []
    assert _events(env, "shared", task_id, "dispatch_skipped")[0]["reason"] == (
        REASON_NAMESPACE_MISMATCH
    )

    # On our own board the same fallback still assigns and spawns as before.
    env = fleet(boards={"em-admin": "em"})
    with env.kbc.connect_closing(board="em-admin") as conn:
        task_id = env.kb.create_task(conn, title="unassigned", assignee=None)
    env.as_install("em")
    res = _tick(env, "em-admin", default_assignee="default")
    assert res.auto_assigned_default == [task_id]
    assert _task(env, "em-admin", task_id)["assignee"] == "default"


def test_has_spawnable_is_board_aware(fleet):
    """Another install's queue must not be reported as spawnable work here, or
    a healthy board reads as stuck."""
    env = fleet(boards={"shared": "yummi", "em-admin": "em"})
    _create(env, "shared", title="hers", assignee="default")
    _create(env, "em-admin", title="ours", assignee="default")

    env.as_install("em")
    with env.kbc.connect_closing(board="shared") as conn:
        assert env.kbd.has_spawnable_ready(conn, board="shared") is False
    with env.kbc.connect_closing(board="em-admin") as conn:
        assert env.kbd.has_spawnable_ready(conn, board="em-admin") is True

    env.as_install("yummi")
    with env.kbc.connect_closing(board="shared") as conn:
        assert env.kbd.has_spawnable_ready(conn, board="shared") is True


def test_per_profile_cap_keyed_on_resolved_profile(fleet):
    """``em:default`` and a bare ``default`` in this namespace share one budget
    instead of pretending to be two profiles."""
    env = fleet(boards={"em-admin": "em"})
    _create(env, "em-admin", title="bare", assignee="default")
    _create(env, "em-admin", title="explicit", assignee="em:default")

    env.as_install("em")
    res = _tick(env, "em-admin", dry_run=True, max_in_progress_per_profile=1)
    assert len(res.spawned) == 1
    assert len(res.skipped_per_profile_capped) == 1
    assert res.skipped_per_profile_capped[0][2] == 1


def test_resolve_assignee_is_pure_and_inspectable(fleet):
    """The resolution is a value, not a side effect — the whole gate is one
    question (``claimable``) plus a remedy for the human."""
    env = fleet(boards={"shared": "yummi", "em-admin": "em"})

    env.as_install("em")
    assert resolve_assignee("default", "em-admin").claimable
    own = resolve_assignee("default", "em-admin")
    assert own.profile == "default" and own.board_namespace == "em"
    foreign = resolve_assignee("default", "shared")
    assert not foreign.claimable and foreign.reason == REASON_NAMESPACE_MISMATCH
    override = resolve_assignee("yummi:admin", "em-admin")
    assert not override.claimable and override.reason == REASON_NAMESPACE_MISMATCH
    assert resolve_assignee("em:admin", "shared").profile == "admin"
    assert resolve_assignee("nobody:admin", "em-admin").reason == REASON_NAMESPACE_MISMATCH
    undeclared = resolve_assignee("default", "default")
    assert undeclared.claimable and undeclared.board_namespace is None


def test_display_name_never_derives_the_namespace(fleet):
    """The token lives in config and is authoritative; ``display_name`` is
    cosmetic and user-editable, so editing it must not re-scope anything."""
    env = fleet(boards={"shared": "em"})
    os.makedirs(os.path.join(env.home, "profiles", "default"), exist_ok=True)
    with open(os.path.join(env.home, "profiles", "default", "profile.yaml"), "w") as fh:
        fh.write("display_name: Yummi\n")
    with open(os.path.join(env.home, "config.yaml"), "w") as fh:
        fh.write("kanban:\n  namespace: em\n")
    env.monkeypatch.delenv("HERMES_KANBAN_NAMESPACE", raising=False)

    assert install_namespaces() == ("em", frozenset({"em"}))
    # The mismatch between display_name and token is expected and harmless.
    assert resolve_assignee("default", "shared").claimable
    assert not resolve_assignee("yummi:default", "shared").claimable


def test_namespace_aliases_accept_configured_tokens(fleet):
    """Comma-separated tokens are accepted as aliases (the OS-user fallback is
    a real case: an install configured as ``em`` may still be swept while a
    stale token is in play) while ONE canonical token is named in messages."""
    env = fleet(boards={"shared": "deprecated"})
    env.monkeypatch.setenv("HERMES_KANBAN_NAMESPACE", "Em, deprecated")
    assert install_namespaces() == ("em", frozenset({"em", "deprecated"}))
    assert resolve_assignee("default", "shared").claimable
    assert resolve_assignee("Deprecated:default", "shared").claimable
