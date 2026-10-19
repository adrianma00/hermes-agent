"""kanban_precomplete — the PRE-COMPLETE gate for kanban cards.

WHY  ``complete`` used to be a bare status flip: the card said "done" and nothing checked that the
     record said so. Three real failures from one night, all invisible at completion time:
       * a cross-linked card was closed without the counterpart card being updated (silent
         completion — the initiator never learned);
       * a card whose text mentions ``landing_guard`` carried no parseable approval, so the
         dashboard's release button silently disappeared (nobody noticed the button was missing);
       * ``🔗 ns#pending`` tokens that never aged into a real id.
     Completion is the transition where that drift becomes permanent, so the check belongs here:
     a convention people can walk past will be walked past, a transition that FAILS is answered.

WHAT IS CHECKED (completion only — ``create`` is deliberately NOT gated, because several legitimate
cards discuss the token syntax in prose)
  1. ``approval_malformed`` — the card (title, body or a comment) mentions ``landing_guard``,
     ``--approve`` or "landing approval", but carries no ``approve_sha``. An approval is a
     STRUCTURED field and nothing else (R55): a body/comment line ``approve_sha=<sha>``
     (+ optional ``approve_note=``) at column 0. A ``landing_guard.py --approve <sha> --note "…"``
     command quoted in prose is an INSTRUCTION, never an approval — em-hq's reader that used to
     parse it is gone (P5), so accepting it here would hide a missing release button.
  2. ``unresolved_crosslink`` — a crosslink declaration that cannot be resolved: no ``id=`` at all,
     a placeholder (``pending``) older than :data:`DEFAULT_PENDING_GRACE_S` (900 s = three reporter
     cycles), a line not in the canonical positional form (R55), or an ``id=`` that is simply not
     on the board the line names. A board this install cannot read is a NAMED note, never a refusal.
     Only ``CROSSLINK:`` body lines declare links: body prose, a 🔗 token and the card TITLE all
     contribute nothing (the title shim retired with P5, together with em-hq's reader for it).
  3. ``loop_not_closed`` — the card carries a RESOLVED crosslink and states no
     ``LOOP-CLOSED: board=… id=… via=comment|close|dm at=…`` line, or its claim fails verification:
     the counterpart must have actually changed (status/comment/event) at or after THIS RUN's
     ``started_at``. A ``LOOP-CLOSED`` sentence is a claim; only the counterpart's own rows prove it.

MODES (``EMHQ_GATE_MODE``)
  ``enforce`` (default) refuse a failing completion, naming the check and the remedy.
  ``warn``    log ``WOULD-REFUSE`` and allow. For re-observing the gate after a change.
  ``off``     the single kill-switch: the gate is bypassed entirely, one restart away from a bad
              cut-over being reverted, with no code change. The bypass is itself logged.

FORCE  ``hermes kanban complete <id> --force "<reason>"`` (CLI) or ``force_reason`` on the
  ``kanban_complete`` tool. Always available, and NEVER silent: every forced completion is logged
  with the reason and with the checks it overrode.

LIMITS  stated plainly. A gate that oversells itself is worse than none:
  * Counterparts on a PEER INSTALL with no inbound access from here (mac, surface) cannot be
    verified. They pass with an explicit ``unverifiable: peer-install`` note in the log — never a
    silent pass — and a card that closes one of those is not actually proven.
  * A REMOTE install (heimdall) IS verified over its ssh lane; if that read fails it degrades to
    ``unverifiable: remote-install``. An unreadable board is never reported as clean.
  * Display names are not identities: ``mac``/``surface`` are matched BEFORE any local board of a
    similar name, because a ``<peer>-board`` directory in this install is a local leftover, not the
    peer's board, and reading it would "verify" against the wrong file.

LOG  every attempt — allowed, refused, forced, degraded — appends one line to
  ``~/.hermes/logs/pre-complete-gate.log`` (override: ``EMHQ_GATE_LOG``), in the same shape as
  ``landing_guard``'s log.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as kb

# --- configuration (every knob is an env var so a bad cut-over needs no code change) -------------

GATE_MODE_ENV = "EMHQ_GATE_MODE"
VALID_MODES = ("enforce", "warn", "off")
DEFAULT_MODE = "enforce"

DEFAULT_PENDING_GRACE_S = 900          # 3 reporter cycles: the ingest interval that clears a placeholder
DEFAULT_LOG = "~/.hermes/logs/pre-complete-gate.log"
DEFAULT_PEER_INSTALLS = "mac,surface"  # no inbound access from here -> unverifiable: peer-install
DEFAULT_REMOTE_INSTALLS = "heimdall"   # reachable over the ssh lane -> verify, degrade to remote-install
DEFAULT_REMOTE_SSH = "~/.hermes/bin/nanopi-ssh"
REMOTE_TIMEOUT_S = 25

# --- vocabulary ---------------------------------------------------------------------------------

# R55: approval is a STRUCTURED pair of fields, parsed by position at line start — never the
# human-typed ``landing_guard.py --approve …`` command, which is prose. em-hq's reporter derives the
# release button from these same declarations (reporter/report.py, P5), so "the gate is happy but no
# button appears" is impossible by construction — there is no second grammar for them to diverge on.
APPROVE_SHA_LINE_RE = re.compile(r"^approve_sha\s*[:=]\s*([0-9a-f]{7,40})\s*$", re.IGNORECASE)
APPROVE_NOTE_LINE_RE = re.compile(r"^approve_note\s*[:=]\s*(.+?)\s*$", re.IGNORECASE)
LANDING_MENTION = ("landing_guard", "--approve", "landing approval", "approve_sha")

# R55 grammar, parsed BY POSITION and only at column 0: board, then id, then role
# (`CROSSLINK:`), board, id, via, at (`LOOP-CLOSED:`). A line that does not fit is a DECLARATION
# THAT CANNOT BE PARSED — reported, never guessed at. A 🔗 token means nothing anywhere: not in
# prose, and no longer in the title either (the title parity shim retired with P5).
CROSSLINK_STRICT_RE = re.compile(
    r"^CROSSLINK:\s+board=(\S+)(?:\s+id=(\S+))?(?:\s+role=(\S+))?(?:\s+\S.*)?$")
LOOP_STRICT_RE = re.compile(
    r"^LOOP-CLOSED:\s+board=(\S+)\s+id=(\S+)\s+via=(\S+)\s+at=(\S+)(?:\s+\S.*)?$")
LOOP_VIA = ("comment", "close", "dm")

# Placeholders are honest at creation and must not STAY: `pending` gets the grace window; a line
# with NO id at all cannot be resolved by anyone and is refused immediately.
PENDING_IDS = ("pending", "#pending")
MISSING_IDS = ("", "tbd", "none", "-")

# ``--force`` keeps its old boolean meaning (override the live-claim guard) AND gains an optional
# reason for the pre-complete gate: ``--force`` alone -> no reason supplied, ``--force "why"`` ->
# that text. argparse expresses both with ``nargs="?"`` + a const, and this sentinel is the const.
BARE_FORCE = "__bare_force_no_reason__"


def force_reason_from_cli(force) -> Optional[str]:
    """``--force`` value -> the gate's ``force_reason`` (None = not forced)."""
    if force is None:
        return None
    if force is True or force == BARE_FORCE:
        return ""
    return str(force)


CHECK_APPROVAL = "approval_malformed"
CHECK_PENDING = "unresolved_crosslink"
CHECK_LOOP = "loop_not_closed"


# --- small pure helpers -------------------------------------------------------------------------

def _kv(text: str) -> dict[str, str]:
    """``k=v`` pairs on one line — kept for callers that only need a lenient view."""
    return {m.group(1).lower(): m.group(2)
            for m in re.finditer(r"([A-Za-z_]+)\s*=\s*([A-Za-z0-9_.:/+#-]+)", text or "")}


def _iso(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(ts)))


def mentions_landing(*texts: str) -> bool:
    blob = "\n".join(t or "" for t in texts).lower()
    return any(k in blob for k in LANDING_MENTION)


def find_approval(comments: list[str], body: str) -> Optional[dict]:
    """The card's landing approval: the structured ``approve_sha=`` declaration, or nothing.

    Newest comment first, then body — the order a human would read it. ``source`` is always
    ``structured``; it stays in the dict because callers/log lines name it, but there is no second
    grammar left to accept (P5 retired the legacy command fallback with em-hq's reader for it).
    """
    texts = list(reversed(comments or [])) + [body or ""]
    for text in texts:
        for line in (text or "").splitlines():
            m = APPROVE_SHA_LINE_RE.match(line)
            if m:
                note = ""
                for cand in (text or "").splitlines():
                    n = APPROVE_NOTE_LINE_RE.match(cand)
                    if n:
                        note = n.group(1)
                        break
                return {"sha": m.group(1), "note": note, "source": "structured"}
    return None


@dataclass(frozen=True)
class Link:
    board: str
    id: str
    role: str = ""
    source: str = ""

    def token(self) -> str:
        return f"{self.board}#{self.id}"


def _crosslink_declarations(body: str) -> tuple[list[Link], list[tuple[str, str]]]:
    """Parse ``CROSSLINK:`` body lines BY POSITION (R55) and report what cannot be parsed.

    Only column-0 lines count: R55 makes the declaration the mechanism, so prose (and an indented
    bullet) contributes nothing. Returns ``(links, problems)`` where a problem is
    ``(kind, line)``: ``'unparseable'`` = not the canonical positional form,
    ``'missing-id'`` = canonical form but no ``id=`` (a link with no referent).
    """
    links: list[Link] = []
    problems: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in (body or "").splitlines():
        line = raw.rstrip()
        if not re.match(r"(?i)^CROSSLINK:", line):
            continue
        m = CROSSLINK_STRICT_RE.match(line)
        if not m:
            problems.append(("unparseable", line.strip()[:160]))
            continue
        board = (m.group(1) or "").strip()
        cid = (m.group(2) or "").strip()
        role = (m.group(3) or "").strip()
        if not cid:
            problems.append(("missing-id", line.strip()[:160]))
            continue
        key = (board.lower(), cid.lower())
        if key in seen:
            continue
        seen.add(key)
        links.append(Link(board, cid, role, "crosslink-line"))
    return links, problems


def parse_crosslinks(title: str, body: str) -> list[Link]:
    """Links of a card: ``CROSSLINK:`` body lines, and nothing else.

    ``title`` is accepted for call-site compatibility and IGNORED: a 🔗 token in a title is prose
    like any other, and em-hq's P0 ingest no longer reads it (both retired together in P5). Body
    prose never contributes either — several legitimate cards quote the token syntax while
    discussing it, and that false positive is what the convention exists to end.
    """
    links, _ = _crosslink_declarations(body)
    return links


def crosslink_problems(body: str) -> list[tuple[str, str]]:
    """``CROSSLINK:`` lines that declare a link the gate (or anyone) cannot resolve."""
    return _crosslink_declarations(body)[1]


def parse_loop_closed(texts: list[str]) -> list[dict]:
    """``LOOP-CLOSED:`` lines from the body and comments — positional, at column 0, never a search."""
    out: list[dict] = []
    for text in texts or []:
        for raw in (text or "").splitlines():
            line = raw.rstrip()
            if not re.match(r"(?i)^LOOP-CLOSED:", line):
                continue
            m = LOOP_STRICT_RE.match(line)
            if not m:
                out.append({"board": "", "id": "", "via": "", "at": "",
                            "malformed": True, "raw": line.strip()[:200]})
                continue
            out.append({
                "board": (m.group(1) or "").strip(),
                "id": (m.group(2) or "").strip(),
                "via": (m.group(3) or "").strip().lower(),
                "at": (m.group(4) or "").strip(),
                "malformed": False,
                "raw": line.strip()[:200],
            })
    return out


def _at_is_a_claim(at: str) -> bool:
    """``at=`` must be a real timestamp, not the template placeholder or a bare word."""
    if not at or "<" in at or at.lower() in ("when done", "pending", "todo", "n/a"):
        return False
    return bool(re.search(r"\d{4}-\d{2}-\d{2}", at))


# --- counterpart resolution ---------------------------------------------------------------------

@dataclass
class Counterpart:
    """What we could learn about the other end of a crosslink."""
    ok: bool = False
    last_change: Optional[float] = None
    status: str = ""
    unverifiable: str = ""      # '' when verified; 'peer-install' | 'remote-install' | 'unknown-board'
    detail: str = ""


def _as_list(value: str) -> list[str]:
    return [v.strip().lower() for v in (value or "").split(",") if v.strip()]


def _board_db(slug: str) -> Optional[Path]:
    """Local kanban.db for a board slug, or None when this install does not hold that board."""
    slug = (slug or "").strip().lower()
    if not slug:
        return None
    if slug == kb.DEFAULT_BOARD:
        path = kb.kanban_home() / "kanban.db"
    else:
        path = kb.boards_root() / slug / "kanban.db"
    return path if path.is_file() else None


def board_slug_from_conn(conn: Optional[sqlite3.Connection]) -> str:
    """Best-effort slug of the board ``conn`` is open on (its own file decides, not the cwd)."""
    if conn is not None:
        try:
            for row in conn.execute("PRAGMA database_list"):
                path = row[2]
                if not path:
                    continue
                p = Path(path)
                if p.name == "kanban.db" and p.parent.parent == kb.boards_root():
                    return p.parent.name
                if p.parent == kb.kanban_home():
                    return kb.DEFAULT_BOARD
        except sqlite3.Error:
            pass
    try:
        return kb.get_current_board()
    except Exception:                                       # pragma: no cover - never wedge a gate
        return kb.DEFAULT_BOARD


_REMOTE_SNIPPET = r'''
import glob, json, os, sqlite3, sys
tid = sys.argv[1]
paths = [os.path.expanduser("~/.hermes/kanban.db")]
paths += sorted(glob.glob(os.path.expanduser("~/.hermes/kanban/boards/*/kanban.db")))
out = {"found": False}
for p in paths:
    con = None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
        row = con.execute(
            "SELECT status, created_at, started_at, completed_at FROM tasks WHERE id=?",
            (tid,)).fetchone()
        if not row:
            continue
        c = con.execute("SELECT MAX(created_at) FROM task_comments WHERE task_id=?",
                        (tid,)).fetchone()[0]
        e = con.execute("SELECT MAX(created_at) FROM task_events WHERE task_id=?",
                        (tid,)).fetchone()[0]
        stamps = [x for x in (row[1], row[2], row[3], c, e) if x]
        out = {"found": True, "status": row[0],
               "last_change": max(stamps) if stamps else None, "db": p}
        break
    except Exception as exc:
        out = {"found": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
print(json.dumps(out))
'''


class BoardResolver:
    """Turns ``(board, card_id)`` into a :class:`Counterpart`, or a NAMED unverifiable reason.

    Classification order matters: peer and remote installs are matched BEFORE local boards, so a
    local ``mac-board`` leftover can never be read as the Mac's board.
    """

    def __init__(self, *, own_slug: str = "", peer_installs: Optional[str] = None,
                 remote_installs: Optional[str] = None, ssh_helper: Optional[str] = None,
                 timeout_s: float = REMOTE_TIMEOUT_S):
        self.own_slug = (own_slug or "").strip().lower()
        self.peer = _as_list(peer_installs if peer_installs is not None
                             else os.environ.get("EMHQ_GATE_PEER_INSTALLS", DEFAULT_PEER_INSTALLS))
        self.remote = _as_list(remote_installs if remote_installs is not None
                               else os.environ.get("EMHQ_GATE_REMOTE_INSTALLS",
                                                   DEFAULT_REMOTE_INSTALLS))
        self.ssh_helper = Path(ssh_helper or os.environ.get("EMHQ_GATE_REMOTE_SSH",
                                                           DEFAULT_REMOTE_SSH)).expanduser()
        self.timeout_s = timeout_s

    # -- namespace -> candidate slugs -----------------------------------------------------------
    def _candidates(self, board: str) -> list[str]:
        ns = (board or "").strip().lower()
        if not ns:
            return []
        cands: list[str] = []
        if ns in ("em", "em-board", "self") and self.own_slug:
            cands.append(self.own_slug)         # `em` names the namespace, not a board slug
        cands.append(ns)
        if not ns.endswith("-board"):
            cands.append(f"{ns}-board")         # `yummi` -> `yummi-board`
        if self.own_slug and ns == self.own_slug:
            cands.append(ns)
        out: list[str] = []
        for c in cands:
            if c and c not in out:
                out.append(c)
        return out

    def resolve(self, board: str, card_id: str) -> Counterpart:
        ns = (board or "").strip().lower()
        if ns in self.remote:
            got = self._remote(card_id)
            if got is not None:
                return got
            return Counterpart(unverifiable="remote-install",
                               detail=f"ssh read of {ns} did not answer ({self.ssh_helper})")
        if ns in self.peer:
            return Counterpart(unverifiable="peer-install",
                               detail=f"{ns} has no inbound access from this install")
        for slug in self._candidates(board):
            path = _board_db(slug)
            if path is None:
                continue
            got = self._local(path, card_id)
            if got is not None:
                return got
        return Counterpart(unverifiable="unknown-board",
                           detail=f"no readable board for {board!r} on this install")

    # -- readers -------------------------------------------------------------------------------
    def _local(self, path: Path, card_id: str) -> Optional[Counterpart]:
        con = None
        try:
            con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT status, created_at, started_at, completed_at FROM tasks WHERE id=?",
                (card_id,)).fetchone()
            if row is None:
                return Counterpart(ok=False, detail=f"{card_id} not on {path.parent.name}")
            cm = con.execute("SELECT MAX(created_at) FROM task_comments WHERE task_id=?",
                             (card_id,)).fetchone()[0]
            ev = con.execute("SELECT MAX(created_at) FROM task_events WHERE task_id=?",
                             (card_id,)).fetchone()[0]
            stamps = [row["created_at"], row["started_at"], row["completed_at"], cm, ev]
            stamps = [s for s in stamps if s]
            return Counterpart(ok=True, status=row["status"] or "",
                               last_change=max(stamps) if stamps else None)
        except sqlite3.Error as exc:
            return Counterpart(unverifiable="unreadable-board",
                               detail=f"{path.parent.name}: {exc}")
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass

    def _remote(self, card_id: str) -> Optional[Counterpart]:
        """Read the far install's boards over its ssh lane. None = could not look (degrade)."""
        if not self.ssh_helper.is_file():
            return None
        payload = base64.b64encode(_REMOTE_SNIPPET.encode()).decode()
        cmd = f"echo {payload} | base64 -d | python3 - {card_id}"
        try:
            proc = subprocess.run([str(self.ssh_helper), cmd], check=False,
                                  capture_output=True, text=True, timeout=self.timeout_s)
        except (OSError, subprocess.SubprocessError):
            return None
        line = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        try:
            data = json.loads(line[0])
        except ValueError:
            return None
        if not data.get("found"):
            return Counterpart(ok=False, detail=f"{card_id} not found on the remote install")
        return Counterpart(ok=True, status=data.get("status") or "",
                           last_change=data.get("last_change"))


# --- the decision -------------------------------------------------------------------------------

@dataclass
class Finding:
    check: str
    detail: str
    remedy: str


@dataclass
class Evaluation:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


def evaluate(*, title: str, body: str, comments: list[str], resolver: BoardResolver,
             run_started_at: float, card_created_at: Optional[float] = None,
             now: Optional[float] = None,
             pending_grace_s: float = DEFAULT_PENDING_GRACE_S) -> Evaluation:
    """Pure check of one card. No IO beyond ``resolver``; every finding names a remedy."""
    now = time.time() if now is None else now
    ev = Evaluation()
    title, body, comments = title or "", body or "", list(comments or [])

    # 1. approval_malformed
    if mentions_landing(title, body, *comments):
        approval = find_approval(comments, body)
        if approval is None:
            ev.findings.append(Finding(
                CHECK_APPROVAL,
                "the card mentions landing_guard / --approve / 'landing approval' but carries no "
                "approve_sha — a quoted command is an instruction, not an approval",
                "write the structured declaration on its own line at column 0 (R55): "
                "approve_sha=<sha> (optionally approve_note=<who/what>). That declaration IS the "
                "approval the dashboard derives its release button from; quoting "
                "`landing_guard.py --approve <sha> --note \"…\"` proves nothing and no longer "
                "produces a button."))

    # 2. unresolved_crosslink — a declaration that cannot be resolved
    for kind, line in crosslink_problems(body):
        if kind == "missing-id":
            ev.findings.append(Finding(
                CHECK_PENDING,
                f"CROSSLINK line names no id — the link has no referent: {line!r}",
                "name the real counterpart card: `CROSSLINK: board=<slug> id=<card_id> "
                "role=tracker|work|peer` (an id is `t_` + hex; `pending` is honest only briefly)."))
        else:
            ev.findings.append(Finding(
                CHECK_PENDING,
                f"CROSSLINK line is not in the canonical positional form: {line!r}",
                "rewrite it at column 0, fields in order — `CROSSLINK: board=<slug> "
                "id=<card_id> role=tracker|work|peer`. Field order is the grammar: a 🔗 token, a "
                "retitled card, or reordered `id=`/`board=` declares nothing the gate (or the "
                "dashboard) can resolve."))

    links = parse_crosslinks(title, body)
    anchor = card_created_at or run_started_at
    loop_lines = parse_loop_closed([body] + comments)
    resolved: list[tuple[Link, Counterpart]] = []
    for link in links:
        cid = link.id.strip().lower()
        if cid in MISSING_IDS or cid in PENDING_IDS:
            age = now - float(anchor)
            if age > pending_grace_s:
                ev.findings.append(Finding(
                    CHECK_PENDING,
                    f"{link.token()} is still a placeholder after {int(age)}s "
                    f"(> {int(pending_grace_s)}s grace, from the card's creation)",
                    f"resolve {link.token()} to the real id (edit the CROSSLINK: line — the title "
                    f"is not read) and, if the peer owns that card, ask it to send the id; "
                    f"then complete."))
            continue
        cp = resolver.resolve(link.board, link.id)
        if cp.unverifiable:
            ev.notes.append(f"unverifiable: {cp.unverifiable} {link.token()} — {cp.detail}")
            continue
        if not cp.ok:
            ev.findings.append(Finding(
                CHECK_PENDING,
                f"{link.token()} does not resolve on board {link.board!r} "
                f"({cp.detail or 'no such card'})",
                f"fix the id/board in the CROSSLINK: line — {link.token()} names no card, so the "
                f"loop can never be closed on it. Complete the correct counterpart, then "
                f"complete this card."))
            continue
        resolved.append((link, cp))

    # 3. loop_not_closed — a resolved link must be PROVEN closed on the counterpart
    for link, cp in resolved:
        cands = [c for c in loop_lines if not c["malformed"]
                 and c["board"].lower() == link.board.lower()
                 and c["id"].lower() == link.id.lower()]
        usable = [c for c in cands if c["via"] in LOOP_VIA and _at_is_a_claim(c["at"])]
        # Newest usable claim wins: a card's body may still carry the template placeholder while a
        # later comment carries the real line — the first line found must not poison the proof.
        claim = usable[-1] if usable else None
        if claim is None:
            broken = next((c for c in cands), None) or next(
                (c for c in loop_lines if c["malformed"]
                 and (link.id.lower() in c["raw"].lower()
                      or link.token().lower() in c["raw"].lower())), None)
            if broken is not None:
                ev.findings.append(Finding(
                    CHECK_LOOP,
                    f"LOOP-CLOSED for {link.token()} is malformed: {broken['raw']!r} "
                    f"(via={broken['via']!r} at={broken['at']!r})",
                    f"rewrite the line at column 0, fields in order: LOOP-CLOSED: "
                    f"board={link.board} id={link.id} via={'|'.join(LOOP_VIA)} at=<iso8601> — "
                    f"'via' and a real timestamp are required."))
                continue
            ev.findings.append(Finding(
                CHECK_LOOP,
                f"{link.token()} is resolved but the card states no LOOP-CLOSED line for it",
                f"update {link.token()} (comment on it, or move it on its own board) and then add "
                f"a line to this card: LOOP-CLOSED: board={link.board} id={link.id} "
                f"via=comment|close|dm at=<iso8601>"))
            continue
        if claim["via"] not in LOOP_VIA or not _at_is_a_claim(claim["at"]):
            ev.findings.append(Finding(
                CHECK_LOOP,
                f"LOOP-CLOSED for {link.token()} is malformed (via={claim['via']!r} "
                f"at={claim['at']!r})",
                f"rewrite the line as LOOP-CLOSED: board={link.board} id={link.id} "
                f"via={'|'.join(LOOP_VIA)} at=<iso8601> — 'via' and a real timestamp are required."))
            continue
        if not cp.ok:
            ev.findings.append(Finding(
                CHECK_LOOP,
                f"LOOP-CLOSED claims {link.token()} was closed, but {cp.detail or 'it could not be read'}",
                f"check the id/board in the LOOP-CLOSED line — {link.token()} does not resolve. "
                f"Fix the line or the link, then complete."))
            continue
        if cp.last_change is None or float(cp.last_change) < float(run_started_at):
            ev.findings.append(Finding(
                CHECK_LOOP,
                f"LOOP-CLOSED claims {link.token()} was updated, but its last change is "
                f"{_iso(cp.last_change)} — before this run started ({_iso(run_started_at)}); "
                f"status={cp.status or 'unknown'}",
                f"actually update {link.token()} now (comment on it, or close it on its own "
                f"board), then re-check: the counterpart must change at or after this run's start. "
                f"If the update is genuinely impossible (peer-owned card), record it with "
                f"--force \"<reason>\" so the override is logged."))
        else:
            ev.notes.append(f"loop closed: {link.token()} changed {_iso(cp.last_change)} "
                            f"via={claim['via']}")
    return ev


# --- the wired gate (IO: reads the card, logs, returns a verdict) --------------------------------

@dataclass
class Verdict:
    allowed: bool
    check: str = ""
    message: str = ""
    mode: str = DEFAULT_MODE
    forced: bool = False
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # A refusal must never be mistaken for a terminal failure: nothing changed on the card.
    @property
    def refusal(self) -> bool:
        return not self.allowed and not self.forced


def resolve_mode(mode: Optional[str] = None) -> str:
    raw = (mode if mode is not None else os.environ.get(GATE_MODE_ENV, "")).strip().lower()
    return raw if raw in VALID_MODES else DEFAULT_MODE


def _log_path(log_path: Optional[os.PathLike | str] = None) -> Path:
    """Gate log path: ``EMHQ_GATE_LOG`` else ``<hermes home>/logs/pre-complete-gate.log``.

    Resolved through the kanban home (which honours ``HERMES_HOME`` / ``HERMES_KANBAN_HOME``), not
    a hardcoded ``~/.hermes``, so an isolated test or sandbox home never writes into the real one.
    """
    if log_path is not None:
        return Path(log_path).expanduser()
    override = (os.environ.get("EMHQ_GATE_LOG") or "").strip()
    if override:
        return Path(override).expanduser()
    return kb.kanban_home() / "logs" / "pre-complete-gate.log"


def log(line: str, *, log_path: Optional[os.PathLike | str] = None) -> None:
    """Append one line to the gate log. A log failure must NEVER change a verdict."""
    try:
        path = _log_path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {line}\n")
    except Exception:                      # logging is best-effort by contract
        pass


def run_started_at(conn: sqlite3.Connection, task_id: str,
                   task: Optional[object] = None) -> Optional[float]:
    """THIS run's ``started_at`` — from ``task_runs``, not ``tasks``.

    ``tasks.started_at`` is the FIRST claim and is never advanced by a retry, so a card on its
    third attempt would compare the counterpart against a timestamp hours old and pass a loop that
    was never closed. The current run row (falling back to the newest attempt) is the honest
    anchor.
    """
    rows: list[Optional[float]] = []
    try:
        cur = conn.execute(
            "SELECT started_at FROM task_runs WHERE id = (SELECT current_run_id FROM tasks "
            "WHERE id = ?)", (task_id,)).fetchone()
        if cur and cur[0]:
            return float(cur[0])
        latest = conn.execute(
            "SELECT MAX(started_at) FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()
        if latest and latest[0]:
            return float(latest[0])
    except sqlite3.Error:
        pass
    if task is not None:
        for attr in ("started_at", "created_at"):
            value = getattr(task, attr, None)
            if value:
                rows.append(float(value))
    return rows[0] if rows else None


def _card_text(conn: sqlite3.Connection, task_id: str) -> list[str]:
    try:
        return [r[0] or "" for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,))]
    except sqlite3.Error:
        return []


def gate_completion(conn: sqlite3.Connection, task_id: str, *,
                    force_reason: Optional[str] = None, mode: Optional[str] = None,
                    now: Optional[float] = None,
                    log_path: Optional[os.PathLike | str] = None,
                    resolver: Optional[BoardResolver] = None) -> Verdict:
    """The pre-complete gate for one card. Never raises: a broken gate must not wedge the board.

    Returns a :class:`Verdict`; ``allowed`` False means the caller must refuse and show
    ``message`` (which names the failed check and the remedy). ``force_reason`` (any string, even
    empty) marks a deliberate override: allowed, and logged as ``FORCED`` with the reason.
    """
    mode = resolve_mode(mode)
    if mode == "off":
        log(f"BYPASS {task_id} :: {GATE_MODE_ENV}=off (kill-switch) — no checks ran", log_path=log_path)
        return Verdict(allowed=True, mode=mode, notes=["bypassed kill-switch"])

    try:
        task = kb.get_task(conn, task_id)
        if task is None:                      # unknown id: the normal path reports it properly
            return Verdict(allowed=True, mode=mode)
        own_slug = board_slug_from_conn(conn)
        started = run_started_at(conn, task_id, task)
        now = time.time() if now is None else now
        if started is None:
            started = now                   # no run history: nothing older than "now" can be proof
        comments = _card_text(conn, task_id)
        resolver = resolver or BoardResolver(own_slug=own_slug)
        ev = evaluate(title=task.title or "", body=task.body or "", comments=comments,
                      resolver=resolver, run_started_at=started,
                      card_created_at=getattr(task, "created_at", None), now=now,
                      pending_grace_s=float(os.environ.get("EMHQ_GATE_PENDING_GRACE_S")
                                            or DEFAULT_PENDING_GRACE_S))
    except Exception as exc:                  # pragma: no cover - defensive
        log(f"DEGRADED {task_id} :: gate error {type(exc).__name__}: {exc} — allowed", log_path=log_path)
        return Verdict(allowed=True, mode=mode, notes=[f"gate error: {exc}"])

    checks = ", ".join(f.check for f in ev.findings) or "clean"
    for note in ev.notes:
        log(f"NOTE {task_id} board={own_slug} :: {note}", log_path=log_path)

    if force_reason is not None:
        reason = (force_reason or "").strip() or "(no reason given)"
        log(f"FORCED {task_id} board={own_slug} :: reason={reason!r} overrode=[{checks}]",
            log_path=log_path)
        return Verdict(allowed=True, mode=mode, forced=True, findings=ev.findings,
                       notes=ev.notes, message=reason)

    if ev.findings:
        primary = ev.findings[0]
        detail = "; ".join(f"[{f.check}] {f.detail}" for f in ev.findings)
        if mode == "warn":
            log(f"WOULD-REFUSE {task_id} board={own_slug} :: {detail}", log_path=log_path)
            return Verdict(allowed=True, mode=mode, check=primary.check,
                           findings=ev.findings, notes=ev.notes,
                           message=refusal_message(task_id, ev.findings, mode="warn"))
        log(f"REFUSED {task_id} board={own_slug} :: {detail}", log_path=log_path)
        return Verdict(allowed=False, mode=mode, check=primary.check,
                       findings=ev.findings, notes=ev.notes,
                       message=refusal_message(task_id, ev.findings, mode=mode))

    log(f"ALLOWED {task_id} board={own_slug} :: 3 checks clean "
        f"({len(ev.notes)} note(s))", log_path=log_path)
    return Verdict(allowed=True, mode=mode, notes=ev.notes)


def refusal_message(task_id: str, findings: list[Finding], *, mode: str = "enforce") -> str:
    """One refusal, naming WHICH check failed and the exact remedy — never bare 'refused'."""
    lines = [f"kanban: pre-complete gate refused {task_id} — nothing changed, the card is still "
             f"in flight."]
    if mode == "warn":
        lines = [f"kanban: pre-complete gate WOULD refuse {task_id} (warn mode: allowed) — "
                 f"fix these before the gate is enforcing:"]
    for f in findings:
        lines.append(f"  ✗ {f.check}: {f.detail}")
        lines.append(f"    remedy: {f.remedy}")
    lines.append(f"  Every attempt is logged to {_log_path()}.")
    lines.append(f"  Deliberate override (logged, never silent): "
                 f"`hermes kanban complete {task_id} --force \"<reason>\"` "
                 f"(tool: force_reason=\"<reason>\").")
    lines.append(f"  Fleet-wide kill-switch: {GATE_MODE_ENV}=off (then restart the gateway).")
    return "\n".join(lines)
