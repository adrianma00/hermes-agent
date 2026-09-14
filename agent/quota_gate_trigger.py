"""Close the provider-quota gate the moment the primary reports a quota wall.

The gate's own trigger: when a turn fails on a rate-limit/billing error that names a
RESET TIME, the primary's subscription window is exhausted. Rather than let every
subsequent turn, cron job and kanban dispatch re-hit the wall, we write the shared gate
card so the whole fleet holds until the provider says the window reopens.

Deliberately narrow:
  * only fires when ``kanban.quota_gate.enabled`` is true (opt-in, like every other
    gate reader — an install that never enabled it pays nothing and behaves as before)
  * only fires when the error carries a real reset instant (a transient 429 with no
    reset time keeps the existing short-cooldown behaviour)
  * only writes on a transition (gate open -> closed, or a LATER reset) so a fleet
    hammering the same wall doesn't rewrite the card on every attempt
  * never raises: a gate-write failure must not mask the provider error the user
    actually needs to see.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Only these classified reasons mean "the window is exhausted", as opposed to a
# transient throttle that will clear on its own.
_WALL_REASONS = frozenset({"rate_limit", "billing"})


def maybe_close_gate(
    *, provider: Optional[str], reset_at: Optional[float], reason: Optional[str],
    window: Optional[str] = None,
) -> bool:
    """Close the shared quota gate for ``provider`` until ``reset_at``.

    Returns True when the card was written (or refreshed to a later reset).
    """
    if not provider or not reset_at:
        return False
    if reason not in _WALL_REASONS:
        return False
    try:
        reset_at = float(reset_at)
    except (TypeError, ValueError):
        return False
    if reset_at <= time.time():
        return False

    try:
        from hermes_cli.kanban_db import read_quota_gate_state, write_quota_gate_card, _quota_gate_config

        enabled, _board = _quota_gate_config()
        if not enabled:
            return False

        state = read_quota_gate_state(enabled=True)
        if state.is_closed and state.reset_at and state.reset_at >= reset_at:
            # Already holding for at least this long — nothing to add.
            return False

        write_quota_gate_card(str(provider), {
            "v": 1,
            "provider": str(provider),
            "window": window or "5h",
            "closed_at": int(time.time()),
            "reset_at": int(reset_at),
            "opened_at": None,
            "source": "first-429",
        })
        logger.warning(
            "Provider-quota gate CLOSED for %s until %s (reason=%s) — kanban dispatch "
            "and cron jobs on this provider will hold until the window reopens.",
            provider, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(reset_at)), reason,
        )
        return True
    except Exception:
        logger.debug("quota gate: close-on-429 failed (non-fatal)", exc_info=True)
        return False
