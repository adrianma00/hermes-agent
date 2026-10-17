"""Handoff-summary previews must look like previews, not like complete sentences.

Fork customization (``FORK-CUSTOMIZATIONS.md`` §11): upstream's ``_first_line``
kept only line 1 and hard-cut at the limit with **no marker**, so a 1079-char
card handoff surfaced as a 400-char fragment that read as a finished sentence.
The full text was never lost (``task_runs.summary`` keeps it) -- nothing said
the preview was partial, and nothing rendered the rest.

Invariants pinned here:

1. ``_preview`` never exceeds its limit, always ends in ``…`` when it cuts, and
   never cuts in the middle of a word (hard cut only when the window holds no
   word boundary at all).
2. Newlines collapse instead of being dropped: a multi-line handoff keeps its
   content past line 1.
3. The ``completed`` / ``edited`` / ``review_requested`` event payloads keep the
   400-char cap (notifications consume them) but advertise the full length in
   ``summary_chars``, so a consumer can tell "truncated" from "short".
4. ``kanban show`` / ``kanban runs`` render the stored handoff **verbatim** -- a
   1079-char summary is fully visible, not cut at 160/100 chars.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kcli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

LIMITS = (40, 100, 160, 200, 400)

# > 400 chars of ordinary prose-ish words separated by single spaces.
LONG_PROSE = " ".join(f"word{i:03d}" for i in range(200))
# A multi-line handoff whose second line carries the real content.
MULTILINE = "Implemented the fix.\nDetails: preview truncates silently; join lines now.\nTests: 8 passed."
# One token with no boundary inside the window (exercises the hard-cut fallback).
LONG_TOKEN = "x" * 900


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.init_db()
    return home


def _flat(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# 1. _preview: bounded, marked, word-safe
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("limit", LIMITS)
@pytest.mark.parametrize("text", [LONG_PROSE, MULTILINE, LONG_TOKEN])
def test_preview_bounded_marked_and_never_mid_word(text: str, limit: int) -> None:
    preview = kb._preview(text, limit)
    flat = _flat(text)

    assert len(preview) <= limit
    if len(flat) <= limit:
        # Fits: returned as-is, and adding "…" would falsely imply a cut.
        assert preview == flat
        return
    assert preview.endswith("…")
    body = preview[:-1]
    # Nothing was invented and nothing was reordered: it is a prefix of the text.
    assert flat.startswith(body)
    window = flat[: max(0, limit - 1)]
    if " " in window:
        # A boundary existed inside the window -> the cut lands on it, so the
        # character right after the preview is a space, never a word character.
        assert flat[len(body)] == " "


@pytest.mark.parametrize("limit", LIMITS)
def test_preview_uses_the_whole_window_for_prose(limit: int) -> None:
    """A cut must be honest about being a cut, and must not waste the budget."""
    preview = kb._preview(LONG_PROSE, limit)
    assert len(preview) >= limit - 12  # last word boundary, not the first one


def test_preview_short_text_is_returned_verbatim_and_unmarked() -> None:
    assert kb._preview("short handoff", 400) == "short handoff"
    assert kb._preview(f"{'y' * 400}", 400) == "y" * 400  # exactly at the limit: no cut


def test_preview_empty_inputs() -> None:
    assert kb._preview(None, 400) == ""
    assert kb._preview("", 400) == ""
    assert kb._preview("   \n\t ", 400) == ""
    assert kb._preview("anything", 0) == ""


def test_preview_keeps_content_beyond_line_one() -> None:
    """The old helper dropped lines 2+ silently; joining keeps them."""
    preview = kb._preview(MULTILINE, 400)
    assert "\n" not in preview
    assert preview == _flat(MULTILINE)
    assert "join lines now" in preview and "Tests: 8 passed." in preview


def test_preview_multiline_truncation_counts_chars_not_lines() -> None:
    preview = kb._preview(MULTILINE, 40)
    assert len(preview) <= 40
    assert preview.endswith("…")
    # Line-2 content still fits in the budget instead of being dropped outright.
    assert "Details" in preview or "preview" in preview


def test_preview_hard_cut_only_when_no_boundary_in_window() -> None:
    preview = kb._preview(LONG_TOKEN, 20)
    assert preview == f"{'x' * 19}…"  # hard cut, still marked
    assert len(preview) == 20


# ---------------------------------------------------------------------------
# 2. Event payloads: 400-char cap kept, truncation now detectable
# ---------------------------------------------------------------------------

def _events(conn, tid: str, kind: str | None = None) -> list[tuple[str, dict]]:
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
    ).fetchall()
    out = [(r["kind"], json.loads(r["payload"]) if r["payload"] else {}) for r in rows]
    return [e for e in out if e[0] == kind] if kind else out


def _new_claimed_task(conn, title: str = "handoff card") -> str:
    tid = kb.create_task(conn, title=title, assignee="worker")
    if kb.get_task(conn, tid).status != "running":
        assert kb.claim_task(conn, tid, profile="worker") is not None
    return tid


def test_completed_payload_caps_preview_but_reports_full_length(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _new_claimed_task(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.complete_task(conn, tid, summary=LONG_PROSE, expected_run_id=run_id)

        payload = _events(conn, tid, "completed")[-1][1]
        assert payload["summary"].endswith("…")
        assert len(payload["summary"]) <= 400
        # Line-1-only is gone: the preview is a prefix of the collapsed text.
        assert _flat(LONG_PROSE).startswith(payload["summary"][:-1])
        assert payload["summary_chars"] == len(LONG_PROSE)
        assert payload["summary_chars"] > len(payload["summary"])

        # The stored run keeps the full handoff -- previews are the only cut.
        stored = conn.execute(
            "SELECT summary FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()["summary"]
        assert stored == LONG_PROSE


def test_completed_payload_multiline_keeps_line_two(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _new_claimed_task(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.complete_task(conn, tid, summary=MULTILINE, expected_run_id=run_id)

        payload = _events(conn, tid, "completed")[-1][1]
        assert payload["summary"] == _flat(MULTILINE)
        assert "Details" in payload["summary"]
        assert payload["summary_chars"] == len(MULTILINE)


def test_review_requested_and_edited_payloads_use_the_same_preview(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _new_claimed_task(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(conn, tid, summary=LONG_PROSE, reviewer="reviewer",
                                 expected_run_id=run_id)
        payload = _events(conn, tid, "review_requested")[-1][1]
        assert payload["summary"].endswith("…") and len(payload["summary"]) <= 400
        assert payload["summary_chars"] == len(LONG_PROSE)

        # ``edited`` re-backfills a summary on a completed card.
        assert kb.complete_task(conn, tid, summary="short handoff")
        assert kb.edit_task(conn, tid, result="done", summary=LONG_PROSE)
        payload = _events(conn, tid, "edited")[-1][1]
        assert payload["summary"].endswith("…") and len(payload["summary"]) <= 400
        assert payload["summary_chars"] == len(LONG_PROSE)


# ---------------------------------------------------------------------------
# 3. CLI surfaces render the full stored handoff
# ---------------------------------------------------------------------------

def _ns(tid: str, **extra) -> argparse.Namespace:
    base = dict(task_id=tid, state_type=None, state_name=None, json=False)
    base.update(extra)
    return argparse.Namespace(**base)


def _completed_card_with_long_handoff(handoff: str) -> str:
    with kbc.connect() as conn:
        tid = _new_claimed_task(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.complete_task(conn, tid, summary=handoff, expected_run_id=run_id)
    return tid


def test_cmd_runs_prints_the_full_summary(kanban_home: Path, capsys) -> None:
    handoff = LONG_PROSE
    tid = _completed_card_with_long_handoff(handoff)
    capsys.readouterr()
    assert kcli._cmd_runs(_ns(tid)) == 0
    out = capsys.readouterr().out
    assert handoff in out  # verbatim, all 1000+ chars -- not [:100]
    assert "…" not in out


def test_cmd_show_prints_the_full_summary(kanban_home: Path, capsys) -> None:
    handoff = MULTILINE
    tid = _completed_card_with_long_handoff(handoff)
    capsys.readouterr()
    assert kcli._cmd_show(_ns(tid)) == 0
    out = capsys.readouterr().out
    assert "join lines now" in out  # line 2 used to be invisible in Runs:
    assert "Tests: 8 passed." in out
