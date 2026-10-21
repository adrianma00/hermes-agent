"""``ready_nonempty()`` must answer per BOARD, not against the pinned namespace.

The embedded dispatcher raises a health line when the ready queue looks non-empty
but no worker spawns:

    kanban dispatcher stuck: ready queue non-empty for N consecutive ticks but
    0 workers spawned. Check profile health (venv, PATH, credentials)

``ready_nonempty()`` walks the boards and asks each whether it holds spawnable
work. It asked WITHOUT naming the board, so the assignee was resolved against
the pinned/first board's namespace instead of the board being iterated. A card
on a SHARED board that belongs to another install — ``yummi-board``'s bare
``default``, whose board namespace is ``yummi`` — therefore resolved as
claimable under this install's namespace and the probe answered True on every
tick, forever.

The consequences are the pair this telemetry exists to avoid: a false alarm that
never clears ("stuck" on a correct queue), and — because a bad tick keeps the
counter pinned — a real stall on that install would never be distinguishable
from it.
"""

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_boards as kbb
from hermes_cli import kanban_db_dispatch as kbd
from gateway import kanban_watchers_dispatcher as kdw


def _dispatcher():
    """A dispatcher bare enough to enumerate boards (no adapters, no settings)."""
    disp = kdw._KanbanDispatcher.__new__(kdw._KanbanDispatcher)
    disp.kb = kb
    return disp


@pytest.fixture
def cross_namespace_home(tmp_path, monkeypatch):
    """This install claims ``em``; a second board declares ``other``.

    ``default`` is a bare assignee that must mean ``em:default`` on OUR board and
    nothing at all on ``shared`` — that asymmetry is the whole test.
    """
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_NAMESPACE", "em")
    # Profile existence is environment, not the behaviour under test: assert the
    # shape "this name is a profile here" without needing a real install.
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda name: True))

    kbb.create_board("own", namespace="em")
    kbb.create_board("shared", namespace="other")

    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect(board="shared")
    try:
        kb.create_task(conn, title="another install's work", assignee="default")
    finally:
        conn.close()
    return tmp_path


def _add_card(board: str, title: str) -> None:
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect(board=board)
    try:
        kb.create_task(conn, title=title, assignee="default")
    finally:
        conn.close()


def test_ready_nonempty_ignores_a_foreign_card_on_a_shared_board(cross_namespace_home):
    """The card is ready and assigned, but it is not this install's to spawn."""
    assert _dispatcher().ready_nonempty() is False


def test_the_same_card_on_our_own_board_reads_spawnable(cross_namespace_home):
    """Control: the probe sees this exact card shape when the board is ours.

    Without this, a False above could mean "empty fixture" rather than
    "correctly scoped"; here the identical card on a board of our own namespace
    makes the probe answer True.
    """
    _add_card("own", "our own work")
    assert _dispatcher().ready_nonempty() is True
