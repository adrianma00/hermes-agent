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
     ``--approve`` or "landing approval", but no approval the dashboard can parse is present.
     The parser is the SAME one em-hq uses (:data:`APPROVE_RE`), on purpose: a gate that accepts
     what the dashboard cannot read would hide the missing button it exists to catch.
  2. ``unresolved_crosslink`` — a ``🔗 <scope>#pending`` token older than
     :data:`DEFAULT_PENDING_GRACE_S` (900 s = three reporter cycles).
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

# Mirrors em-hq's reporter (reporter/report.py:42). Same regex on purpose: the dashboard decides
# whether to offer a release with exactly this, so "the gate is happy but no button appears" is
# impossible by construction.
APPROVE_RE = re.compile(r'landing_guard\.py\s+--approve\s+([0-9a-f]{7,40})\s+--note\s+"([^"\n]*)"')
LANDING_MENTION = ("landing_guard", "--approve", "landing approval")

LINK_RE = re.compile(r"🔗\s*([A-Za-z0-9_-]+)\s*#\s*([A-Za-z0-9_-]+)")
CROSSLINK_LINE_RE = re.compile(r"^\s*(?:[-*]\s+)?CROSSLINK:", re.IGNORECASE)
LOOP_LINE_RE = re.compile(r"^\s*(?:[-*]\s+)?LOOP-CLOSED:", re.IGNORECASE)
KV_RE = re.compile(r"([A-Za-z_]+)\s*=\s*([A-Za-z0-9_.:/+-]+)")
LOOP_VIA = ("comment", "close", "dm")

PENDING_IDS = ("pending",)

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
    """``k=v`` pairs on one line. Order does not matter; unknown keys are kept, unread."""
    return {m.group(1).lower(): m.group(2) for m in KV_RE.finditer(text or "")}


def _iso(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(ts)))


def mentions_landing(*texts: str) -> bool:
    blob = "\n".join(t or "" for t in texts).lower()
    return any(k in blob for k in LANDING_MENTION)


def find_approval(comments: list[str], body: str) -> Optional[dict]:
    """Newest comment first, then the body — the order a human would read it (mirrors em-hq)."""
    for text in list(reversed(comments or [])) + [body or ""]:
        m = APPROVE_RE.search(text or "")
        if m:
            return {"sha": m.group(1), "note": m.group(2)}
    return None


@dataclass(frozen=True)
class Link:
    board: str
    id: str
    role: str = ""
    source: str = ""

    def token(self) -> str:
        return f"{self.board}#{self.id}"


def parse_crosslinks(title: str, body: str) -> list[Link]:
    """A crosslink counts only from the card TITLE or a body line that STARTS with ``CROSSLINK:``.

    Body prose contributes nothing: several legitimate cards quote ``🔗 ns#pending`` while
    discussing the syntax, and treating that as a link is the false positive this convention
    exists to avoid. Rules mirror the em-hq P0 parser: optional leading whitespace, optional
    ``- ``/``* `` list marker, ``CROSSLINK:`` at line start (case-insensitive); on such a line both
    the structured ``board=<slug> id=<card_id>`` form and a ``🔗`` token count.
    """
    out: list[Link] = []
    seen: set[tuple[str, str]] = set()

    def add(board: str, cid: str, role: str, source: str) -> None:
        key = ((board or "").strip().lower(), (cid or "").strip().lower())
        if not key[0] or not key[1] or key in seen:
            return
        seen.add(key)
        out.append(Link(board.strip(), cid.strip(), role.strip(), source))

    for m in LINK_RE.finditer(title or ""):
        add(m.group(1), m.group(2), "", "title")
    for line in (body or "").splitlines():
        if not CROSSLINK_LINE_RE.match(line):
            continue
        kv = _kv(line)
        if kv.get("board") and kv.get("id"):
            add(kv["board"], kv["id"], kv.get("role", ""), "crosslink-line")
        for m in LINK_RE.finditer(line):
            add(m.group(1), m.group(2), "", "crosslink-line")
    return out


def parse_loop_closed(texts: list[str]) -> list[dict]:
    """``LOOP-CLOSED:`` lines from the body and comments. Positional, never a prose search."""
    out: list[dict] = []
    for text in texts or []:
        for line in (text or "").splitlines():
            if not LOOP_LINE_RE.match(line):
                continue
            kv = _kv(line)
            out.append({
                "board": (kv.get("board") or "").strip(),
                "id": (kv.get("id") or "").strip(),
                "via": (kv.get("via") or "").strip().lower(),
                "at": (kv.get("at") or "").strip(),
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
    if mentions_landing(title, body, *comments) and not find_approval(comments, body):
        ev.findings.append(Finding(
            CHECK_APPROVAL,
            "the card mentions landing_guard / --approve / 'landing approval' but carries no "
            "approval the dashboard can parse (landing_guard.py --approve <sha> --note \"…\")",
            "approve the landing before completing (one verb writes both the approval file and "
            "the card's record), or drop the landing language from the card. The dashboard reads "
            "landing_guard.py --approve <sha> --note \"<who/what>\" and nothing else, so without "
            "it the release button silently disappears."))

    # 2. unresolved_crosslink
    links = parse_crosslinks(title, body)
    anchor = card_created_at or run_started_at
    for link in links:
        if link.id.strip().lower() not in PENDING_IDS:
            continue
        age = now - float(anchor)
        if age > pending_grace_s:
            ev.findings.append(Finding(
                CHECK_PENDING,
                f"{link.token()} is still a placeholder after {int(age)}s "
                f"(> {int(pending_grace_s)}s grace, from the card's creation)",
                f"resolve {link.token()} to the real id (retitle the card / edit the CROSSLINK: "
                f"line) and, if the peer owns that card, ask it to send the id; then complete."))

    # 3. loop_not_closed — a resolved link must be PROVEN closed on the counterpart
    loop_lines = parse_loop_closed([body] + comments)
    resolved = [l for l in links if l.id.strip().lower() not in PENDING_IDS]
    for link in resolved:
        cp = resolver.resolve(link.board, link.id)
        if cp.unverifiable:
            ev.notes.append(f"unverifiable: {cp.unverifiable} {link.token()} — {cp.detail}")
            continue
        claim = next((c for c in loop_lines
                      if c["board"].lower() == link.board.lower()
                      and c["id"].lower() == link.id.lower()), None)
        if claim is None:
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
