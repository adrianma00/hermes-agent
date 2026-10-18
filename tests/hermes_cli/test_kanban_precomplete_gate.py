"""Tests for the pre-complete gate (``hermes_cli.kanban_precomplete``).

The gate can refuse a legitimate completion, so this matrix IS the safety net: every branch the
decision can take is exercised, plus the wiring (``hermes kanban complete`` refuses; ``--force``
overrides and is logged). Both the pure ``evaluate`` path and the real ``gate_completion`` path
(which reads the card out of a board DB and writes the log) are covered — a decision function
tested only directly cannot see a defect in the layer that fetches its input.
"""
from __future__ import annotations

import argparse
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_precomplete as g


# --- fixtures -----------------------------------------------------------------------------------

@pytest.fixture
def khome(tmp_path, monkeypatch):
    """An isolated kanban home: no real board, no real gate log, no inherited mode."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kh"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("EMHQ_GATE_MODE", raising=False)
    monkeypatch.delenv("EMHQ_GATE_PENDING_GRACE_S", raising=False)
    monkeypatch.delenv("EMHQ_GATE_PEER_INSTALLS", raising=False)
    monkeypatch.setenv("EMHQ_GATE_LOG", str(tmp_path / "gate.log"))
    kb.init_db()
    return tmp_path


@pytest.fixture
def gate_log(khome):
    return khome / "gate.log"


def _conn():
    from hermes_cli import kanban_db_connect as kbc
    return kbc.connect()


def _touch_run(conn, tid: str, started_at: int) -> None:
    conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
                 (tid, "default", "running", started_at))
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, tid))


def mk(conn, *, title: str, body: str = "", comments: tuple[str, ...] = (),
       age_s: int = 0) -> str:
    """Create a card whose run STARTED ``age_s`` seconds ago (so ``run_started_at`` is real)."""
    tid = kb.create_task(conn, title=title, body=body, assignee="default")
    now = int(time.time()) - age_s
    conn.execute("UPDATE tasks SET created_at = ?, started_at = ? WHERE id = ?", (now, now, tid))
    # create_task writes a `created` event at real time; backdate it so "created age_s ago" means
    # the card really has been untouched since then (that is what the loop check reads).
    conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ?", (now, tid))
    _touch_run(conn, tid, now)
    for text in comments:
        kb.add_comment(conn, tid, "worker", text)
    conn.commit()
    return tid


def bump(conn, tid: str, when: int | None = None) -> int:
    """The counterpart changed (a comment lands — what the gate reads). Default: now."""
    kb.add_comment(conn, tid, "worker", "updated by the counterpart")
    conn.commit()
    if when is not None:
        conn.execute("UPDATE task_comments SET created_at = ? WHERE task_id = ?", (when, tid))
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ?", (when, tid))
        conn.commit()
        return when
    return int(conn.execute("SELECT MAX(created_at) FROM task_comments WHERE task_id = ?",
                            (tid,)).fetchone()[0])


def iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


# --- 1. no-link card ----------------------------------------------------------------------------

def test_no_link_card_allowed(khome, gate_log):
    conn = _conn()
    tid = mk(conn, title="Plain card", body="Nothing cross-linked here.")
    v = g.gate_completion(conn, tid)
    assert v.allowed and v.check == "" and not v.findings
    assert "ALLOWED" in gate_log.read_text()
    conn.close()


# --- 2. resolved link with the loop closed ------------------------------------------------------

def test_resolved_link_loop_closed_allowed(khome, gate_log):
    conn = _conn()
    other = mk(conn, title="counterpart card")
    tid = mk(conn, title="Dispatcher card",
             body=f"CROSSLINK: board=default id={other} role=work", age_s=60)
    stamp = bump(conn, other)                       # counterpart changed AFTER this run started
    kb.add_comment(conn, tid, "worker",
                   f"LOOP-CLOSED: board=default id={other} via=comment at={iso(stamp)}")
    conn.commit()
    v = g.gate_completion(conn, tid)
    assert v.allowed, v.message
    log = gate_log.read_text()
    assert "ALLOWED" in log and "loop closed:" in log
    conn.close()


# --- 3. resolved link with the loop OPEN --------------------------------------------------------

def test_resolved_link_loop_open_refused(khome, gate_log):
    conn = _conn()
    other = mk(conn, title="counterpart card", age_s=600)     # untouched since before our run
    tid = mk(conn, title="Dispatcher card",
             body=f"CROSSLINK: board=default id={other} role=work\n"
                  f"LOOP-CLOSED: board=default id={other} via=comment at={iso(int(time.time()))}",
             age_s=60)
    v = g.gate_completion(conn, tid)
    assert not v.allowed
    assert v.check == g.CHECK_LOOP
    assert "before this run started" in v.message
    assert "REFUSED" in gate_log.read_text()
    conn.close()


def test_resolved_link_without_loop_closed_line_refused(khome, gate_log):
    conn = _conn()
    other = mk(conn, title="counterpart card")
    tid = mk(conn, title="Dispatcher card",
             body=f"CROSSLINK: board=default id={other} role=work", age_s=60)
    bump(conn, other)                               # updated, but nothing on the card claims it
    v = g.gate_completion(conn, tid)
    assert not v.allowed and v.check == g.CHECK_LOOP
    assert "states no LOOP-CLOSED line" in v.message
    conn.close()


def test_loop_closed_template_placeholder_refused(khome, gate_log):
    """``at=<fill when done>`` is a template, not a claim."""
    conn = _conn()
    other = mk(conn, title="counterpart card")
    tid = mk(conn, title="Dispatcher card",
             body=f"CROSSLINK: board=default id={other} role=work\n"
                  f"LOOP-CLOSED: board=default id={other} via=comment at=<fill when done>",
             age_s=60)
    bump(conn, other)
    v = g.gate_completion(conn, tid)
    assert not v.allowed and v.check == g.CHECK_LOOP
    assert "malformed" in v.message
    conn.close()


def test_resolved_link_pointing_at_a_missing_card_refused(khome, gate_log):
    conn = _conn()
    tid = mk(conn, title="Dispatcher card",
             body="CROSSLINK: board=default id=t_000000dead role=work\n"
                  f"LOOP-CLOSED: board=default id=t_000000dead via=close at={iso(int(time.time()))}",
             age_s=60)
    v = g.gate_completion(conn, tid)
    assert not v.allowed and v.check == g.CHECK_LOOP
    assert "does not resolve" in v.message
    conn.close()


# --- 4/5. pending link: inside and past the grace window ----------------------------------------

def test_pending_link_inside_grace_allowed(khome, gate_log):
    conn = _conn()
    tid = mk(conn, title="🔗 surface#pending: ask the peer", age_s=120)
    v = g.gate_completion(conn, tid)
    assert v.allowed, v.message
    assert v.findings == []
    conn.close()


def test_pending_link_past_grace_refused(khome, gate_log):
    conn = _conn()
    tid = mk(conn, title="🔗 mac#pending: ask the peer", age_s=g.DEFAULT_PENDING_GRACE_S + 60)
    v = g.gate_completion(conn, tid)
    assert not v.allowed and v.check == g.CHECK_PENDING
    assert "still a placeholder" in v.message
    assert "REFUSED" in gate_log.read_text()
    conn.close()


def test_pending_grace_is_configurable(khome, gate_log, monkeypatch):
    monkeypatch.setenv("EMHQ_GATE_PENDING_GRACE_S", "60")
    conn = _conn()
    tid = mk(conn, title="🔗 mac#pending: ask the peer", age_s=120)
    v = g.gate_completion(conn, tid)
    assert not v.allowed and v.check == g.CHECK_PENDING
    conn.close()


# --- 6. malformed approval ----------------------------------------------------------------------

def test_landing_mention_without_approval_refused(khome, gate_log):
    conn = _conn()
    tid = mk(conn, title="Land the fix",
             body="Merge to main once landing_guard approves; then --approve it.")
    v = g.gate_completion(conn, tid)
    assert not v.allowed and v.check == g.CHECK_APPROVAL
    assert "no approval the dashboard can parse" in v.message
    assert "approval_malformed" in gate_log.read_text()
    conn.close()


def test_landing_mention_with_approval_allowed(khome, gate_log):
    sha = "a" * 40
    conn = _conn()
    tid = mk(conn, title="Land the fix", body="landing_guard gate applies.",
             comments=(f'approved: landing_guard.py --approve {sha} --note "Adrian approved"',))
    v = g.gate_completion(conn, tid)
    assert v.allowed, v.message
    conn.close()


def test_approval_must_match_the_dashboard_parser(khome, gate_log):
    """A bare sha with no ``--note`` is NOT something em-hq can read, so it is malformed here too."""
    conn = _conn()
    tid = mk(conn, title="Land the fix",
             body="approved by landing_guard --approve " + "b" * 40)
    v = g.gate_completion(conn, tid)
    assert not v.allowed and v.check == g.CHECK_APPROVAL
    conn.close()


# --- 7. forced override -------------------------------------------------------------------------

def test_force_overrides_and_is_logged(khome, gate_log):
    conn = _conn()
    tid = mk(conn, title="Land the fix", body="landing_guard approves this.")
    v = g.gate_completion(conn, tid, force_reason="approved out of band by Adrian")
    assert v.allowed and v.forced
    log = gate_log.read_text()
    assert "FORCED" in log
    assert "approved out of band by Adrian" in log
    assert "approval_malformed" in log              # what was overridden is recorded too
    conn.close()


def test_bare_force_is_still_logged_as_forced(khome, gate_log):
    conn = _conn()
    tid = mk(conn, title="Land the fix", body="landing_guard approves this.")
    v = g.gate_completion(conn, tid, force_reason="")
    assert v.allowed and v.forced
    assert "(no reason given)" in gate_log.read_text()
    conn.close()


# --- 8. peer-unverifiable -----------------------------------------------------------------------

def test_peer_install_link_passes_with_explicit_note(khome, gate_log):
    """Mac/Surface cards are on their own disk: pass, but never silently."""
    conn = _conn()
    tid = mk(conn, title="🔗 surface#t_abc123: peer work",
             body="CROSSLINK: board=mac id=t_def456 role=peer",
             age_s=60)
    v = g.gate_completion(conn, tid)
    assert v.allowed, v.message
    log = gate_log.read_text()
    assert "unverifiable: peer-install surface#t_abc123" in log
    assert "unverifiable: peer-install mac#t_def456" in log
    conn.close()


def test_unknown_board_is_unverifiable_not_silent(khome, gate_log):
    conn = _conn()
    tid = mk(conn, title="cross-fleet", body="CROSSLINK: board=nowhere id=t_zzz role=work")
    v = g.gate_completion(conn, tid)
    assert v.allowed
    assert "unverifiable: unknown-board nowhere#t_zzz" in gate_log.read_text()
    conn.close()


# --- modes: kill-switch and warn ----------------------------------------------------------------

def test_off_mode_bypasses_and_logs(khome, gate_log, monkeypatch):
    monkeypatch.setenv("EMHQ_GATE_MODE", "off")
    conn = _conn()
    tid = mk(conn, title="Land the fix", body="landing_guard approves this.")
    v = g.gate_completion(conn, tid)
    assert v.allowed and v.mode == "off"
    assert "BYPASS" in gate_log.read_text()
    conn.close()


def test_warn_mode_allows_but_logs_would_refuse(khome, gate_log, monkeypatch):
    monkeypatch.setenv("EMHQ_GATE_MODE", "warn")
    conn = _conn()
    tid = mk(conn, title="Land the fix", body="landing_guard approves this.")
    v = g.gate_completion(conn, tid)
    assert v.allowed and v.mode == "warn" and v.findings
    assert "WOULD-REFUSE" in gate_log.read_text()
    conn.close()


def test_default_mode_is_enforce(monkeypatch):
    monkeypatch.delenv("EMHQ_GATE_MODE", raising=False)
    assert g.resolve_mode() == "enforce"
    monkeypatch.setenv("EMHQ_GATE_MODE", "nonsense")
    assert g.resolve_mode() == "enforce"          # an unreadable knob must not quietly disable it


def test_unwritable_log_never_changes_the_verdict(khome, tmp_path):
    """Logging is best-effort BY CONTRACT: an unwritable log must not turn a refusal into a pass,
    nor crash the completion path. (It did crash once — this is the regression guard.)"""
    conn = _conn()
    tid = mk(conn, title="Land the fix", body="landing_guard approves this.")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    v = g.gate_completion(conn, tid, log_path=blocker / "sub" / "gate.log")
    assert not v.allowed and v.check == g.CHECK_APPROVAL
    conn.close()


# --- parser agreement with the dashboard --------------------------------------------------------

def test_crosslinks_ignore_body_prose():
    """A card that QUOTES the token syntax is not linked (the P0 false positive)."""
    links = g.parse_crosslinks("no link here", "we should stop writing 🔗 ns#pending in prose")
    assert links == []
    links = g.parse_crosslinks("plain", "  - CROSSLINK: board=em-board id=t_abc role=work")
    assert [(l.board, l.id) for l in links] == [("em-board", "t_abc")]
    links = g.parse_crosslinks("🔗 em#t_1: work", "")
    assert [(l.board, l.id) for l in links] == [("em", "t_1")]


def test_kv_parsing_handles_reordered_fields():
    links = g.parse_crosslinks("", "CROSSLINK: id=t_abc role=tracker board=em-board")
    assert [(l.board, l.id, l.role) for l in links] == [("em-board", "t_abc", "tracker")]


# --- run anchor: the CURRENT attempt, not the first claim ---------------------------------------

def test_loop_closure_must_postdate_the_current_run_not_the_first_claim(khome, gate_log):
    """A retry's ``started_at`` comes from task_runs; ``tasks.started_at`` never advances."""
    conn = _conn()
    other = mk(conn, title="counterpart card", age_s=7200)
    stamp = bump(conn, other, when=int(time.time()) - 1200)   # old: before THIS run started
    tid = mk(conn, title="Dispatcher card",
             body=f"CROSSLINK: board=default id={other} role=work\n"
                  f"LOOP-CLOSED: board=default id={other} via=comment at={iso(stamp)}",
             age_s=7200)
    _touch_run(conn, tid, int(time.time()) - 60)              # the retry that is completing now
    conn.commit()
    v = g.gate_completion(conn, tid)
    assert not v.allowed and v.check == g.CHECK_LOOP
    assert "before this run started" in v.message
    conn.close()


def test_run_anchor_falls_back_to_the_newest_attempt(khome):
    conn = _conn()
    tid = mk(conn, title="card", age_s=7200)
    conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (tid,))
    conn.commit()
    anchor = g.run_started_at(conn, tid, kb.get_task(conn, tid))
    assert anchor is not None and abs(anchor - (time.time() - 7200)) < 5
    conn.close()


# --- wiring: the CLI actually refuses, and --force actually overrides ---------------------------

def _parser():
    from hermes_cli.kanban import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return parser


def test_cli_complete_refuses_then_force_overrides(khome, gate_log, capsys):
    from hermes_cli.kanban import kanban_command

    conn = _conn()
    tid = mk(conn, title="Land the fix", body="landing_guard approves this.")
    conn.close()

    parser = _parser()
    rc = kanban_command(parser.parse_args(["kanban", "complete", tid, "--summary", "done"]))
    assert rc != 0
    out = capsys.readouterr()
    shown = out.err + out.out
    assert "pre-complete gate" in shown and g.CHECK_APPROVAL in shown

    conn = _conn()
    assert kb.get_task(conn, tid).status != "done"
    conn.close()

    rc = kanban_command(parser.parse_args(
        ["kanban", "complete", tid, "--summary", "done", "--force", "no landing needed here"]))
    assert rc == 0
    conn = _conn()
    assert kb.get_task(conn, tid).status == "done"
    conn.close()
    log = gate_log.read_text()
    assert "FORCED" in log and "no landing needed here" in log


def test_cli_bare_force_keeps_its_boolean_meaning():
    """``--force`` with no value must still mean 'override the live-claim guard' (arity guard)."""
    args = _parser().parse_args(["kanban", "complete", "t_abc", "--force"])
    assert args.force == g.BARE_FORCE


def test_cli_force_reason_is_captured():
    args = _parser().parse_args(["kanban", "complete", "t_abc", "--force", "because"])
    assert args.force == "because"
