"""Whether something will respawn this serve/dashboard after it exits.

The code-skew watchdog (``web_server_skew_exit``) retires a backend whose checkout
moved underneath it. That is only safe if something brings it back, so the watchdog
was gated on the two cases where the answer was known: a Desktop-owned serve (the app
respawning it) and an SSH-isolated one (its owning client doing so). A serve run by a
process supervisor is a third case the gate never covered — and the one that hurts,
because the host's updater does not know the unit exists and so nobody restarts it. A
systemd dashboard served ``6e4d10829c`` for two days while the checkout sat on a newer
revision, refusing model switches the whole time.

Two signals, cheapest first, and neither guesses per-OS:

``supervision_marker()`` reads what a supervisor stamped into the environment. It is
definitive when present (systemd stamps ``INVOCATION_ID``/``SYSTEMD_EXEC_PID`` on every
unit process) and absent for a shell-started one. launchd and the Windows service
manager stamp nothing, so absence proves nothing — and is never read as proof.

``respawn proof`` is the OS-agnostic answer for those. Every exit writes a timestamp and
every boot compares itself to it: a graceful exit followed by a boot on the same port
within ``RESPAWN_WINDOW_S`` IS a supervisor respawning us, whoever it was. The evidence
accumulates from restarts that happen anyway (an update, a service restart, a client
reconnect), so no deliberate risky probe is needed — which matters, because the failure
mode of a wrong guess here is a backend that exits and stays gone.

A backend with neither signal does not retire itself. It is a manual ``hermes dashboard``
in a terminal as far as the evidence shows, and killing the user's window silently would
be worse than the skew.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

# A supervisor's restart delay lives here. `RestartSec=5`, launchd's throttle and a
# Windows service recovery action are all seconds. A human restarting a dashboard by
# hand, or a client dialling in, takes longer than two minutes; a box that was off takes
# much longer. Generous enough to survive a slow cold boot, tight enough to stay evidence.
RESPAWN_WINDOW_S = 120.0

_STATE_DIR = "state"
_STATE_FILE = "serve-supervision.json"


def supervision_marker() -> Optional[str]:
    """Name of the supervisor that stamped this process, or ``None``.

    Only markers that MEAN "a service manager spawned this" are listed. The absence of
    one is not evidence of absence (launchd and Windows set none), so callers must fall
    through to the respawn proof rather than treating ``None`` as unsupervised.
    """
    if os.environ.get("INVOCATION_ID") and os.environ.get("SYSTEMD_EXEC_PID"):
        return "systemd"
    if os.environ.get("SUPERVISOR_ENABLED"):
        return "supervisord"
    return None


def _state_path(home: Optional[Path] = None) -> Path:
    if home is None:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    return Path(home) / _STATE_DIR / _STATE_FILE


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        # Bookkeeping that cannot be written must never take the server down with it;
        # the consequence is only that this boot cannot be used as evidence later.
        _log.debug("Could not record serve supervision: %s", exc)


def note_boot(*, port: Optional[int], pid: Optional[int] = None, home: Optional[Path] = None,
              now: Optional[float] = None) -> dict[str, Any]:
    """Record this process, and claim proof when the previous one was just respawned.

    ``port`` is the bound port: it identifies the *service* across restarts, which a pid
    cannot. ``None`` or 0 (an ephemeral bind) means there is nothing to correlate, so the
    record is still refreshed but no proof is derived.
    """
    if not port:
        return {}

    path = _state_path(home)
    previous = _load(path)
    stamp = time.time() if now is None else now
    pid = os.getpid() if pid is None else pid

    proof = previous.get("proof") if previous.get("port") == port else None
    proc = pid
    if isinstance(proof, dict) and not isinstance(proof.get("at"), (int, float)):
        proof = None

    exited_at = previous.get("exited_at")
    same_service = previous.get("port") == port and previous.get("pid") != proc
    if same_service and isinstance(exited_at, (int, float)):
        gap = stamp - exited_at
        if 0 <= gap <= RESPAWN_WINDOW_S:
            # Graceful exit, then a boot on the same port inside the window: something
            # held this service down and brought it back. That is the whole claim, and it
            # is what makes the watchdog safe to arm on a supervisor we cannot name.
            proof = {"at": stamp, "gap_s": round(gap, 3), "for_pid": previous.get("pid")}
            _log.info("serve supervision proven: port %s came back %.1fs after pid %s exited",
                      port, gap, previous.get("pid"))

    record = {
        "port": port,
        "pid": proc,
        "booted_at": stamp,
        "exited_at": None,
        "exit_reason": None,
        "proof": proof,
    }
    _save(path, record)

    return record


def note_exit(*, port: Optional[int], pid: Optional[int] = None, reason: str = "shutdown",
              home: Optional[Path] = None, now: Optional[float] = None) -> None:
    """Record a planned exit so the next boot can measure the gap.

    Written on the graceful path only. A crash leaves ``exited_at`` null, and the next boot
    then has no tight timestamp to compare against — no proof from a crash, which is the
    conservative direction.
    """
    if not port:
        return

    path = _state_path(home)
    record = _load(path)
    if record.get("port") != port:
        return
    if pid is not None and record.get("pid") != pid:
        return

    record["exited_at"] = time.time() if now is None else now
    record["exit_reason"] = reason
    _save(path, record)


def respawn_expected(*, port: Optional[int], home: Optional[Path] = None) -> tuple[bool, str]:
    """``(expected, evidence)``: will something bring this service back after it exits?

    ``evidence`` is meant for a log line, so an operator can tell a marker from an
    accumulated proof when a backend reports that it is about to retire itself.
    """
    marker = supervision_marker()
    if marker:
        return True, f"marker:{marker}"

    if port:
        proof = _load(_state_path(home)).get("proof")
        if isinstance(proof, dict) and isinstance(proof.get("at"), (int, float)):
            return True, f"proven:{proof.get('gap_s')}s"

    return False, "unknown"


def clear(*, home: Optional[Path] = None) -> None:
    """Test/reset seam."""
    try:
        _state_path(home).unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "RESPAWN_WINDOW_S",
    "clear",
    "note_boot",
    "note_exit",
    "respawn_expected",
    "supervision_marker",
]
