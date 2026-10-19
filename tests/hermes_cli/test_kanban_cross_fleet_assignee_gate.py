"""R33 (cross-fleet assignee discipline) — the creation-side gate.

R33 says cross-fleet work goes to the OTHER fleet's TOP-LEVEL bot, never to one of
its children, and that this must be enforced AT CREATION with a visible refusal
("never a silent rewrite"), because the receiving dispatcher cannot catch it: a
bare child name on the shared board resolves to the board's own namespace, so it
looks like the receiving top bot's legitimate delegation and the child is already
spawned by the time any rule is read.

Live defect this pins (card t_3b982b18, reported by Yummi): three cards created on
the shared board ``yummi-admin`` (namespace ``yummi``) from the ``em`` install with
``assignee="yummi-admin"`` — a Yummi CHILD — each of which had to be performed
read-only and handed to Yummi for a routing decision. The same window also filed
two cards with the bare ``default`` (the correct target) which routed fine, so the
gate must refuse the child form and leave the top-level form alone.

The gate is deliberately narrow; every allowed shape below is asserted, because a
gate that also refuses the correct cross-fleet route would stop fleet work.

Isolation: same recipe as ``test_kanban_per_profile_cap_namespaced.py`` — the
fixture home is built OUTSIDE the platform default root and pinned with
``HERMES_KANBAN_HOME``, and the resolved paths are asserted before the first write
(an isolated home that collapses onto the LIVE boards is the leak t_7ecd88e2 owns).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# This install (the creator) and the other fleet's install.
INSTALL_NS = "em"
FOREIGN_NS = "yummi"
OWN_BOARD = "em-admin"
FOREIGN_BOARD = "yummi-admin"          # namespace declared: FOREIGN_NS
UNDECLARED_BOARD = "plain"             # no namespace declared at all
# The child that was actually misrouted in the live defect.
FOREIGN_CHILD = "yummi-admin"

# Pins the live dispatcher injects into every worker: an inherited one identifies
# THAT process's own board, so a fixture keeping it writes into the live file
# however isolated its home is.
_INHERITED_PINS = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT",
)


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
            continue
        if not (parent.is_dir() and os.access(parent, os.W_OK)):
            continue
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent))).resolve()
    raise RuntimeError(f"no writable temp base outside {native}")


@pytest.fixture()
def fleet(monkeypatch):
    """A hermetic install (namespace ``em``) with a foreign board declared ``yummi``.

    ``env.as_install(ns)`` re-reads the namespace, so a test can also play the
    OTHER install (the side whose own delegation to ``FOREIGN_CHILD`` is legitimate).
    """
    home = _fixture_home("kanban_cross_fleet_gate_")
    for prof in ("default", "sysadmin", FOREIGN_CHILD, "alpha"):
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
    # Fail LOUD before writing anything: a home that resolves onto the live root is
    # the leak t_7ecd88e2 owns.
    resolved = Path(kb.kanban_home()).resolve()
    assert resolved == home, f"kanban_home() resolves to {resolved}, not {home}"

    kb.create_board(slug=OWN_BOARD, name=OWN_BOARD, namespace=INSTALL_NS)
    kb.create_board(slug=FOREIGN_BOARD, name=FOREIGN_BOARD, namespace=FOREIGN_NS)
    kb.create_board(slug=UNDECLARED_BOARD, name=UNDECLARED_BOARD)
    for slug in (OWN_BOARD, FOREIGN_BOARD, UNDECLARED_BOARD):
        board_db = Path(kb.kanban_db_path(slug)).resolve()
        assert home in board_db.parents, f"board {slug} would be written at {board_db}"
    kbd._reset_namespace_cache()

    env = SimpleNamespace(home=home, kb=kb, kbc=kbc, kbd=kbd, FOREIGN_CHILD=FOREIGN_CHILD)

    def as_install(ns: str) -> None:
        monkeypatch.setenv("HERMES_KANBAN_NAMESPACE", ns)
        kbd._reset_namespace_cache()

    env.as_install = as_install
    try:
        yield env
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _create(env, board, *, title, assignee, explicit_board=False):
    """Create a card the way each surface does: ``board=`` (tool/dashboard) or the
    env-pinned current board (CLI)."""
    with env.kbc.connect_closing(board=board) as conn:
        kwargs = {"board": board} if explicit_board else {}
        return env.kb.create_task(conn, title=title, assignee=assignee, **kwargs)


def _task_count(env, board) -> int:
    with env.kbc.connect_closing(board=board) as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


# ---------------------------------------------------------------------------
# The live defect: a foreign fleet's CHILD is refused at creation
# ---------------------------------------------------------------------------


def test_foreign_child_bare_name_on_a_shared_board_is_refused(fleet):
    """The exact live case: ``yummi-admin`` on the board declaring ``yummi``, from ``em``."""
    env = fleet
    with env.kbc.connect_closing(board=FOREIGN_BOARD) as conn:
        with pytest.raises(ValueError) as exc:
            env.kb.create_task(conn, title="Audit your cron jobs", assignee=FOREIGN_CHILD,
                               board=FOREIGN_BOARD)
    msg = str(exc.value)
    # Visible refusal: names the rule (R32 citation form), the reason and the fix.
    assert msg.startswith("R33 (cross-fleet assignee discipline)")
    assert repr(FOREIGN_CHILD) in msg
    assert FOREIGN_NS in msg
    assert "'default'" in msg
    assert "Nothing was created" in msg
    # …and it really created nothing.
    assert _task_count(env, FOREIGN_BOARD) == 0


def test_foreign_child_explicit_namespace_is_refused(fleet):
    """``yummi:<child>`` is the same misroute spelled the canonical way."""
    env = fleet
    with env.kbc.connect_closing(board=UNDECLARED_BOARD) as conn:
        with pytest.raises(ValueError, match="R33"):
            env.kb.create_task(conn, title="explicit child",
                               assignee=f"{FOREIGN_NS}:{FOREIGN_CHILD}",
                               board=UNDECLARED_BOARD)
    assert _task_count(env, UNDECLARED_BOARD) == 0


def test_foreign_child_refused_on_the_env_pinned_cli_path(fleet, monkeypatch):
    """The CLI shape: no ``board=`` argument, the board comes from the env pin.

    ``hermes kanban --board <slug> create`` pins ``HERMES_KANBAN_BOARD`` for the
    call; the gate must resolve the same board the connection was opened on.
    """
    env = fleet
    monkeypatch.setenv("HERMES_KANBAN_BOARD", FOREIGN_BOARD)
    with env.kbc.connect_closing(board=FOREIGN_BOARD) as conn:
        with pytest.raises(ValueError, match="R33"):
            env.kb.create_task(conn, title="pinned", assignee=FOREIGN_CHILD)
    assert _task_count(env, FOREIGN_BOARD) == 0


@pytest.mark.parametrize("spelling", ["Yummi:" + FOREIGN_CHILD, FOREIGN_CHILD.upper()])
def test_gate_is_case_insensitive(fleet, spelling):
    env = fleet
    with env.kbc.connect_closing(board=FOREIGN_BOARD) as conn:
        with pytest.raises(ValueError, match="R33"):
            env.kb.create_task(conn, title="case", assignee=spelling, board=FOREIGN_BOARD)


# ---------------------------------------------------------------------------
# Everything R33 allows must still be creatable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "assignee",
    [
        "default",              # the bare top-level name, resolved via the board
        f"{FOREIGN_NS}:default",  # …and the explicit spelling of the same target
    ],
)
def test_other_fleets_top_level_is_allowed(fleet, assignee):
    """The CORRECT cross-fleet route (R33: that fleet's TOP-LEVEL bot) must not be gated.

    Live evidence this shape works: t_b99281ec / t_c4118245 (assignee ``default``)
    routed to Yummi and closed normally in the same window as the misroutes.
    """
    env = fleet
    task_id = _create(env, FOREIGN_BOARD, title="for the other top bot",
                      assignee=assignee, explicit_board=True)
    assert task_id
    assert _task_count(env, FOREIGN_BOARD) == 1


def test_own_namespace_child_on_a_foreign_board_is_allowed(fleet):
    """This fleet's OWN child on the other install's board is deliberate placement."""
    env = fleet
    task_id = _create(env, FOREIGN_BOARD, title="ours here",
                      assignee=f"{INSTALL_NS}:sysadmin", explicit_board=True)
    assert task_id


def test_own_child_on_own_board_is_allowed(fleet):
    """The receiving side's own delegation shape (R14): the board's namespace IS
    this install's, so a bare child name is a legitimate order from its own top bot."""
    env = fleet
    task_id = _create(env, OWN_BOARD, title="our delegation",
                      assignee="sysadmin", explicit_board=True)
    assert task_id


def test_the_other_install_creating_for_its_own_child_is_allowed(fleet):
    """The very same string is legal on the side that owns it — the gate is
    install-relative, so Yummi filing ``yummi-admin`` on her own board still works."""
    env = fleet
    env.as_install(FOREIGN_NS)
    task_id = _create(env, FOREIGN_BOARD, title="her own child",
                      assignee=FOREIGN_CHILD, explicit_board=True)
    assert task_id


def test_undeclared_board_keeps_name_partitioning(fleet):
    """No declared namespace ⇒ nothing is foreign to refuse (today's behaviour)."""
    env = fleet
    task_id = _create(env, UNDECLARED_BOARD, title="undeclared",
                      assignee=FOREIGN_CHILD, explicit_board=True)
    assert task_id


def test_assignee_without_a_profile_half_is_left_to_the_dispatch_path(fleet):
    """``yummi:`` is not a cross-fleet child; it stays ``assignee_invalid`` at dispatch."""
    env = fleet
    with env.kbc.connect_closing(board=UNDECLARED_BOARD) as conn:
        task_id = env.kb.create_task(conn, title="no profile", assignee=f"{FOREIGN_NS}:",
                                     board=UNDECLARED_BOARD)
    assert task_id


# ---------------------------------------------------------------------------
# The override, for an install whose top-level profile is not named ``default``
# ---------------------------------------------------------------------------


def test_configured_top_level_assignees_widen_the_gate(fleet, monkeypatch):
    """``kanban.cross_fleet_top_level_assignees`` names the other fleet's top bot.

    Same config surface as the rest of the namespace resolution: a deliberate,
    visible edit rather than a per-card bypass.
    """
    env = fleet
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config_readonly",
        lambda *a, **k: {"kanban": {
            "cross_fleet_top_level_assignees": f"default,{FOREIGN_CHILD}"}},
    )
    assert env.kbd.cross_fleet_top_level_assignees() == frozenset({"default", FOREIGN_CHILD})
    task_id = _create(env, FOREIGN_BOARD, title="widened",
                      assignee=FOREIGN_CHILD, explicit_board=True)
    assert task_id
