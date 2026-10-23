"""Auto-decompose must not pay the aux model for a card it cannot decompose.

Covers the two pre-call guards -- a graph already settled for the card's current
text, and a card parked by the block-loop breaker awaiting a human -- plus the
explicit retire-then-rebuild paths (re-scoped text, ``force``). The previously
unguarded path re-billed one aux call per dispatcher tick, forever, for a reply
``decompose_triage_task`` was always going to refuse (#109566 / #118872).

No network: the aux client is mocked.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _resp(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


GRAPH = jsonlib.dumps({
    "fanout": True,
    "rationale": "one child is enough here",
    "tasks": [{"title": "child one", "body": "do a", "assignee": "worker", "parents": []}],
})


def _patch_aux(content: str = GRAPH):
    return patch("agent.auxiliary_client.call_llm", return_value=_resp(content))


def _patch_profiles(names: tuple[str, ...] = ("orchestrator", "worker")):
    fake = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0]),
    ]


def _triage(conn, title="rough idea", body="do the thing"):
    return kb.create_task(conn, title=title, body=body, triage=True)


def _repark(tid: str, body: str | None = None) -> None:
    """Simulate an operator (or the breaker) putting a decomposed root back."""
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
            if body is not None:
                conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, tid))


def _run(tid: str, **kwargs):
    """Call decompose_task with the aux client and roster mocked; return (outcome, aux_mock)."""
    started = _patch_profiles()
    for p in started:
        p.start()
    try:
        with _patch_aux() as aux:
            outcome = decomp.decompose_task(tid, author="tester", **kwargs)
        return outcome, aux
    finally:
        for p in started:
            p.stop()


def test_settled_graph_costs_no_aux_call(kanban_home):
    with kbc.connect() as conn:
        tid = _triage(conn)

    first, aux1 = _run(tid)
    assert first.ok, first.reason
    assert aux1.call_count == 1

    # The dispatcher sees the root back in triage on every tick.
    _repark(tid)
    second, aux2 = _run(tid)
    assert second.ok is False
    assert "already committed" in second.reason
    assert aux2.call_count == 0, "a settled graph must not reach the aux model"


def test_rescoped_text_retires_the_old_graph_then_rebuilds(kanban_home):
    with kbc.connect() as conn:
        tid = _triage(conn)

    first, _ = _run(tid)
    assert first.ok, first.reason
    old_child = first.child_ids[0]

    _repark(tid, body="actually the scope changed completely")
    second, aux2 = _run(tid)

    assert second.ok, second.reason
    assert aux2.call_count == 1
    with kbc.connect() as conn:
        assert kb.get_task(conn, old_child).status == "archived"
        kinds = {r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,))}
        assert "decompose_reset" in kinds
        # The root is linked under the NEW children only -- no second generation.
        parents = {r["parent_id"] for r in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (tid,))}
        assert old_child not in parents
        assert set(second.child_ids) <= parents


def test_force_rebuilds_a_settled_graph(kanban_home):
    with kbc.connect() as conn:
        tid = _triage(conn)

    first, _ = _run(tid)
    assert first.ok, first.reason
    _repark(tid)

    forced, aux2 = _run(tid, force=True)
    assert forced.ok, forced.reason
    assert aux2.call_count == 1
    with kbc.connect() as conn:
        assert kb.get_task(conn, first.child_ids[0]).status == "archived"
        assert "decompose_reset" in {r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,))}


def test_block_loop_park_waits_for_a_human_but_is_not_permanent(kanban_home):
    with kbc.connect() as conn:
        tid = _triage(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "block_loop_detected", {"reason": "looped"})

    parked, aux1 = _run(tid)
    assert parked.ok is False
    assert "block-loop" in parked.reason
    assert aux1.call_count == 0

    # A human touching the card re-arms it: sticky-until-touched, not banned.
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "commented", {"author": "adrian"})

    touched, aux2 = _run(tid)
    assert touched.ok, touched.reason
    assert aux2.call_count == 1


def test_force_bypasses_the_park(kanban_home):
    with kbc.connect() as conn:
        tid = _triage(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "block_loop_detected", {"reason": "looped"})

    forced, aux = _run(tid, force=True)
    assert forced.ok, forced.reason
    assert aux.call_count == 1


def test_legacy_event_without_digest_is_treated_as_settled(kanban_home):
    """Events written before the digest existed cannot prove a re-scope: refuse, don't pay."""
    with kbc.connect() as conn:
        tid = _triage(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "decomposed",
                             {"child_ids": [], "root_assignee": "orchestrator"})

    out, aux = _run(tid)
    assert out.ok is False
    assert "no body digest" in out.reason
    assert aux.call_count == 0

    # --force is the documented way through it.
    forced, aux2 = _run(tid, force=True)
    assert forced.ok, forced.reason
    assert aux2.call_count == 1


def test_decomposed_event_records_the_body_digest(kanban_home):
    with kbc.connect() as conn:
        tid = _triage(conn)

    first, _ = _run(tid)
    assert first.ok, first.reason

    with kbc.connect() as conn:
        payload = jsonlib.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'decomposed'",
            (tid,)).fetchone()["payload"])
        task = kb.get_task(conn, tid)
    assert payload["body_sha"] == decomp.body_sha(task.title, task.body)
