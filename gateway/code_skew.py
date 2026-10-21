"""Detect when the gateway is running stale code after a hot ``git pull``.

The gateway's ``sys.modules`` is frozen at boot.  If the checkout is updated
underneath it, a first-time lazy import can resolve a freshly-pulled module
against a stale cached dependency -> ImportError.  We snapshot the revision at
startup so risky callers (e.g. ``/model`` switching) can refuse with a clear
"restart the gateway" message.  If the revision can't be read (non-git install,
IO error) the boot snapshot stays ``None`` and detection no-ops — never a false positive.
"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_boot_fingerprint: str | None = None


def _fingerprint() -> str | None:
    """Current checkout fingerprint via the CLI's worktree-aware git-rev reader
    (``hermes_cli.main`` is always already imported in a gateway process)."""
    try:
        from hermes_cli.main import _read_git_revision_fingerprint

        return _read_git_revision_fingerprint(_PROJECT_ROOT)
    except Exception:
        return None


def record_boot_fingerprint() -> None:
    """Snapshot the checkout revision at gateway startup (idempotent)."""
    global _boot_fingerprint
    if _boot_fingerprint is None:
        _boot_fingerprint = _fingerprint()


def _short(fingerprint: str) -> str:
    """Render a ``git:<ref>:<sha>`` fingerprint as a compact label."""
    sha = fingerprint.rsplit(":", 1)[-1]
    return sha[:10] if sha and sha != "unresolved" and len(sha) > 10 else (sha or fingerprint)


def current_code_sha() -> str | None:
    """Full SHA for the checkout currently on disk, or None when unresolved."""
    fingerprint = _fingerprint()
    if fingerprint is None:
        return None
    sha = fingerprint.rsplit(":", 1)[-1]
    return sha if sha and sha != "unresolved" else None


def detect_code_skew() -> tuple[str, str] | None:
    """``(boot_rev, disk_rev)`` short labels if the checkout drifted since boot, else ``None``."""
    current = _fingerprint() if _boot_fingerprint is not None else None
    if current is None or current == _boot_fingerprint:
        return None
    return _short(_boot_fingerprint), _short(current)


def health_revisions(boot_commit: str | None) -> dict[str, str | None]:
    """The revision pair every health endpoint publishes.

    ``commit`` is the revision this process BOOTED from — the caller passes it because
    each server already knows its own. ``disk_commit`` is the checkout on disk NOW, read
    at request time.

    The pair exists so a client can decide "stale" by comparing two strings, instead of
    trusting a verdict the backend had to derive. That derivation runs in the process that
    may itself be stale: a backend old enough to be skewed can predate the ``code_skew``
    field entirely, answer 200 without it, and a client that reads a missing verdict as
    "clean" declares a stale process healthy — which is exactly how a dashboard served
    ``6e4d10829c`` for two days while the checkout sat on a newer revision.

    Both keys are always present and may be ``None``. ``None`` means the revision could
    not be resolved (non-git install, IO error) — never "matches". Omitting the key would
    be ambiguous with a build that has no such field, and that ambiguity is the bug.
    """
    try:
        disk_commit = current_code_sha()
    except Exception:
        disk_commit = None
    return {"commit": boot_commit, "disk_commit": disk_commit}
