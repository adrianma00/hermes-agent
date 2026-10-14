"""Provider-quota gate for ``api_server`` turns — the third reader of the shared gate card.

The shared gate card (``quota-gate:<provider>`` on the fleet board) already governs the two
DISPATCHED work sources: ready kanban cards (``hermes_cli.kanban_db_dispatch``) and due cron
jobs (``cron.scheduler_quota``). The ``api_server`` platform carried every inbound peer DM
(``hermes peer dm`` → ``POST /api/sessions/<id>/chat``) and never consulted it, so a peer
message was the ONE path that got neither the hold nor the fallback route: the turn ran on
the walled provider and the caller got the provider's own 429 back, making the peer channel
silently one-way for the whole wall (live: the Mac peer's DM to Em during the 2026-10-10
monthly wall was answered with ``HTTP 429 … monthly usage quota``).

This module makes that third reader behave like the other two:

  * gate open, or gate disabled          → RUN, untouched;
  * closed, switch OFF                   → HOLD: the turn does not run and the caller gets
                                           :func:`hold_text` back as the turn's reply — never
                                           a raw provider error;
  * closed, switch RELEASE-ALL           → ROUTE this turn to the fallback head, for THIS
                                           turn only, and log it with ``source=release_all``;
  * closed, released, no fallback route  → HOLD ``no_fallback_route`` — the route really is
                                           missing — exactly as the kanban dispatcher and the
                                           cron tick do;
  * closed, released, but the turn pins  → HOLD ``pinned_to_walled``: the pin, not a missing
    the walled provider itself              route, is why this turn is not routed;
  * caller (or its session ``/model``) pinned to a provider that is not the walled one
                                         → exempt, RUN untouched.

Three asymmetries with the dispatched sources, each a consequence of what a peer DM IS:

  * The COST window is not consulted here, only the quota gate. A card or a job is deferred
    to the fallback provider's discount hours because nobody is waiting on it; a peer DM is
    the case R37 names — someone else's next step is blocked on this turn — so delaying it to
    save peak-price tokens would be the wrong trade. ``release_all`` on the gate card is the
    operator's authorisation to spend that fallback.
  * A held turn is NOT queued for later, and nothing about it is stored. A card waits on the
    board and a job is re-armed to the reset; an interactive request has no queue on either
    side (``hermes peer dm`` blocks on the HTTP response, and the sending agent is
    fire-and-forget), and a held turn returns before any agent is created — so the inbound
    message is never persisted anywhere. The honest answer is therefore: the turn was not
    run, the message was not stored, re-send it after the reset.
  * A deliberate provider pin is never overridden. A pin at the request, at the session
    (``/model``), or a confirmed Browser runtime lock outlives the wall, exactly as a
    card-level pin does in the dispatcher — it RUNS while its provider is healthy and is HELD
    (never silently re-routed, never burned against the wall) when its provider is the walled
    one. With a confirmed lock the locked provider is not visible here, so the locked turn is
    left alone rather than rerouted.
  * ``/v1/runs`` runs its own lifecycle and calls this module directly (``api_server_runs``);
    one decision function serves both doors.

Audit: a released turn logs one structured line carrying ``source=release_all`` with the route
and the reset instant — the cron reader's precedent, used here because unlike a kanban card an
api_server turn has no task row to hang a ``quota_released`` event on.

EXPIRE-ON-READ (the third hot reader must reap too): this module is the peer half of the same
failure ``a3961f42d4`` fixed for the other two readers. The gate opens by CLOCK — the shared reader
returns ``is_closed=False`` once ``reset_at`` has passed, without touching the board — so a wall
that simply ran out leaves its card behind, and the ``release_all`` riding that card is preserved
into the next wall by ``_carry_gate_policy``. A peer DM against the NEW wall would then be routed
to the fallback on the strength of a switch thrown against the OLD one (exactly what the switch's
placement on the incident card exists to prevent). The two dispatched sources reap an expired card
before trusting it; the peer reader must do the same, or it is the one reader through which the
inheritance still reaches a turn.

Phase 2A/B: all readers now use the local state DB (quota_state.db).  Expired walls are
automatically filtered by the reader (``reset_at > now``), so no reap is needed and the
``release_all`` inheritance problem is gone by construction.  The board card machinery and
all sweep/reap functions have been removed from the codebase.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

RUN = "run"
ROUTE = "route"
HOLD = "hold"

#: The audit marker a released turn is recorded with. Same string the kanban dispatcher writes
#: into its ``quota_released`` event, so one grep finds peer-triggered fallback spend too.
RELEASE_SOURCE = "release_all"

HOLD_PREFIX = "[quota-hold]"


@dataclass(frozen=True)
class QuotaTurnDecision:
    """What one api_server turn may do while the shared gate is closed."""

    action: str
    reason: str
    provider: Optional[str] = None
    model: Optional[str] = None
    reset_at: Optional[float] = None
    walled_provider: Optional[str] = None
    window: Optional[str] = None
    source: str = ""
    #: Whether a fallback route EXISTS for this turn (independent of whether it may be used).
    #: Carried so ``hold_text`` can tell a pinned turn whether dropping the pin is enough, or
    #: whether the route is missing too, without re-reading the gate.
    route_available: bool = False

    @property
    def is_held(self) -> bool:
        return self.action == HOLD

    @property
    def is_routed(self) -> bool:
        return self.action == ROUTE

    def metadata(self) -> Dict[str, Any]:
        """Audit fields for a result payload / log line (never secrets)."""
        return {
            "action": self.action,
            "reason": self.reason,
            "source": self.source,
            "provider": self.provider,
            "model": self.model,
            "walled_provider": self.walled_provider,
            "window": self.window,
            "reset_at": self.reset_at,
            "route_available": self.route_available,
        }


def configured_primary_provider() -> str:
    """The install's configured default provider (``model.provider``), or ``""``.

    Read through the effective-config loader the dispatcher uses, inside the request's
    profile scope, so a profile-scoped turn is judged against ITS provider.
    """
    try:
        from hermes_cli.config_effective import load_user_config_effective

        cfg = load_user_config_effective() or {}
        return str(((cfg.get("model") or {}).get("provider")) or "").strip()
    except Exception:
        logger.debug("api_server quota gate: primary-provider read failed", exc_info=True)
        return ""


#: Minimum interval between expired-gate-card sweeps on the peer path.
#:
#: The kanban dispatcher and the cron tick each sweep once per TICK — a periodic, externally
#: bounded cadence. The peer reader has no tick of its own: its native cadence is "once per
#: inbound peer turn", which is event-driven and unbounded in principle (a burst of DMs is
#: exactly when a per-request read-write board connect would be worst). Bounding the reap by an
#: interval rather than by turn volume gives it a cadence of its own — at most one sweep per
#: minute, however many turns arrive — which is the "sweep on its own cadence" half of the
#: separation ``a3961f42d4`` drew, instead of the "inline a sweep per turn" it warned against.



def gate_state():
    """The shared gate, or ``None`` when the gate is disabled or open.

    ``None`` means "do not gate" — an install that never enabled
    ``kanban.quota_gate`` behaves exactly as before this module existed. The chain rides
    along because ``state.fallback_model`` is otherwise always None, which would read as
    "no fallback route" and hold every turn on a fleet that has one. Same reader, same
    arguments as ``cron.scheduler_quota.gate_state`` and the kanban dispatcher, so the three
    surfaces cannot disagree about the wall or the route.

    Phase 2A: reads from the local state DB; no reap is needed because expired
    walls are automatically filtered by the reader (``reset_at > now``).
    """
    try:
        from hermes_cli.kanban_db import quota_gate_fallback_chain, read_quota_gate_state
    except Exception:
        logger.debug("api_server quota gate: kanban gate reader unavailable — not gating", exc_info=True)
        return None
    try:
        state = read_quota_gate_state(quota_gate_fallback_chain() or [])
    except Exception:
        # A read that RAISES is a broken reader, not a closed gate: an install whose gate
        # module is importable but unhappy must not lose its peer channel. (A card that
        # exists and cannot be read is a different answer, and ``read_quota_gate_state``
        # returns it as ``is_closed=True`` — that one gates.)
        logger.debug("api_server quota gate: state read failed — not gating", exc_info=True)
        return None
    return state if state.is_closed else None


def decide_turn(
    state, *, pinned_provider: Optional[str] = None, session_provider: Optional[str] = None,
    confirmed_runtime_lock: bool = False, primary_provider: Optional[str] = None,
) -> QuotaTurnDecision:
    """Decide one api_server turn against the gate.

    ``pinned_provider`` is the request's own provider (a raw API caller's ``provider``);
    ``session_provider`` the provider of the session's ``/model`` override, if any;
    ``primary_provider`` the install default (resolved lazily when not handed in).
    """
    if state is None or not getattr(state, "is_closed", False):
        return QuotaTurnDecision(RUN, "gate_open")

    fields: Dict[str, Any] = {
        "reset_at": getattr(state, "reset_at", None),
        "walled_provider": getattr(state, "walled_provider", None),
        "window": getattr(state, "window", None),
    }

    # A confirmed Browser runtime lock IS a pin, but its provider is not visible from here
    # (it is resolved deeper, in ``_create_agent``). Leaving the turn alone is the only
    # honest choice: gating it could hold a locked session that is already pinned to a
    # healthy provider, which is exactly the regression this gate must not introduce.
    if confirmed_runtime_lock:
        return QuotaTurnDecision(RUN, "runtime_locked", **fields)

    pin = (pinned_provider or "").strip()
    session_pin = (session_provider or "").strip()
    effective = pin or session_pin or (primary_provider or "").strip() or configured_primary_provider()

    # A turn that would spend a DIFFERENT provider's quota is exempt — the gate only blocks
    # turns that would actually hit the wall. Same test (``is_exempt``) the dispatcher and
    # the cron tick apply to a pinned card/job, and the same "a pin to the walled provider
    # itself still waits" rule.
    if state.is_exempt(pin) or state.is_exempt(session_pin) or state.is_exempt(effective):
        return QuotaTurnDecision(RUN, "exempt", **fields)

    released = bool(getattr(state, "release_all", False))
    fallback_provider = str(getattr(state, "fallback_provider", "") or "").strip()
    fallback_model = str(getattr(state, "fallback_model", "") or "").strip()
    routable = bool(fallback_provider and fallback_model)

    if released:
        # A pin that reached this point can only be a pin to the WALLED provider (a pin to any
        # other provider is exempt, above). It is held whatever the switch says, and it must
        # not be reported as "no fallback route" — the pin is the reason, and saying otherwise
        # sends the caller hunting for a config that is not the problem.
        if pin or session_pin:
            return QuotaTurnDecision(
                HOLD, "pinned_to_walled", route_available=routable, **fields)
        # Authorised AND routable → route this turn. The route is computed per turn and never
        # persisted, so the moment the gate reopens the next turn returns to the primary,
        # exactly like a released card or job.
        if routable:
            return QuotaTurnDecision(
                ROUTE, "released", provider=fallback_provider, model=fallback_model,
                source=RELEASE_SOURCE, route_available=True, **fields)
        return QuotaTurnDecision(
            HOLD, "no_fallback_route", route_available=False, **fields)

    return QuotaTurnDecision(HOLD, "gate_closed", route_available=routable, **fields)


def decide_for_turn(
    *, pinned_provider: Optional[str] = None, session_provider: Optional[str] = None,
    confirmed_runtime_lock: bool = False,
) -> QuotaTurnDecision:
    """Read the gate and decide one turn — the one call the api_server surfaces make."""
    try:
        state = gate_state()
    except Exception:
        logger.debug("api_server quota gate: gate read raised — not gating", exc_info=True)
        state = None
    return decide_turn(
        state, pinned_provider=pinned_provider, session_provider=session_provider,
        confirmed_runtime_lock=confirmed_runtime_lock)


def _relative(seconds: float) -> str:
    if seconds >= 86400:
        return f"in {seconds / 86400:.1f} days"
    if seconds >= 3600:
        return f"in {seconds / 3600:.1f} hours"
    return f"in {max(1, int(seconds // 60))} min"


def hold_text(decision: QuotaTurnDecision) -> str:
    """The reply a held turn returns to its caller instead of a provider error.

    Written to be actionable by BOTH readers of a peer reply: an agent on the other end (the
    turn did not run and the message was NOT stored, so it must re-send after the reset) and a
    human operator (the one command that changes the outcome).

    Every variant has to say the message was not stored. A held turn returns before an agent is
    created (``_create_agent`` is never reached), and the api_server never appends an inbound
    message itself — the row is written by the agent's turn-start persist. So nothing has kept
    the text, and a reply that implied otherwise would leave the peer waiting for an answer
    that no one will produce.
    """
    walled = decision.walled_provider or "the primary provider"
    win = f" [{decision.window} window]" if decision.window else ""
    until = ""
    if isinstance(decision.reset_at, (int, float)):
        left = float(decision.reset_at) - time.time()
        when = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(float(decision.reset_at)))
        until = f" until {when}" + (f" ({_relative(left)})" if left > 0 else "")
    if decision.reason == "pinned_to_walled":
        why = (f"this turn carries a pin to {walled} (the request's `provider`, or the session's "
               "`/model`) and a deliberate pin is never silently re-routed")
        if decision.route_available:
            unblock = ("Re-send it without the pin — drop the request's `provider` field, or "
                       "clear the session `/model` — and it will be routed to the fallback head "
                       "while the switch is RELEASE-ALL.")
        else:
            unblock = ("No fallback route is configured either (`fallback_providers`), so waiting "
                       "for the reset is the only way out.")
    elif decision.reason == "no_fallback_route":
        why = ("the fleet switch is RELEASE-ALL but no fallback route is configured "
               "(`fallback_providers`), so there is nowhere to send this turn")
        unblock = ("Re-send it once the window reopens (do not resend before then), or have the "
                   "operator configure `fallback_providers` so released peer DMs have somewhere "
                   "to go.")
    else:
        why = ("the fleet switch is in HOLD mode, so inbound peer messages wait for the "
               "reset like ready kanban cards and due cron jobs")
        unblock = ("Re-send it once the window reopens (do not resend before then), or have the "
                   "operator run `hermes quota release-all on` to route peer DMs, cards and "
                   "cron jobs to the fallback head for the rest of the wall.")
    return (
        f"{HOLD_PREFIX} This turn was NOT run: {walled}{win} is quota-walled{until}, and {why}. "
        "Nothing was queued and nothing was stored — a held turn stops before an agent is "
        "created, so this message is NOT in the session transcript. "
        f"{unblock} "
        "Operator: `hermes quota status` shows the wall, the reset and the current switch."
    )


def held_result(decision: QuotaTurnDecision, *, muted: bool = False) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """``(result, usage)`` for a held turn, shaped like the api_server's provider-auth reply.

    The same contract as the ``_ProviderAuthResolutionError`` branch: text, zero usage and no
    HTTP error — so every existing caller (session chat, SSE, ``/v1/responses``) delivers the
    explanation rather than a 502/429, and the turn is never mistaken for a failed one that
    should be retried (``result_retry_action`` keys on ``failed``).
    """
    result: Dict[str, Any] = {
        "final_response": hold_text(decision),
        "messages": [], "api_calls": 0, "tools": [],
        "quota_hold": decision.metadata(),
    }
    if muted:
        result["_notification_presentation_suppressed"] = True
    return result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def log_release(decision: QuotaTurnDecision, *, session_id: str = "", door: str = "chat") -> None:
    """Record a released turn — the audit trail for peer-triggered fallback spend."""
    logger.info(
        "api_server quota gate: released %s turn on the fallback route %s/%s "
        "(source=%s session=%s walled=%s window=%s reset_at=%s)",
        door, decision.provider or "?", decision.model or "?", RELEASE_SOURCE,
        session_id or "?", decision.walled_provider or "?", decision.window or "?",
        decision.reset_at,
    )


def log_hold(decision: QuotaTurnDecision, *, session_id: str = "", door: str = "chat") -> None:
    """Record a held turn: the peer channel being one-way again is the silent failure this
    whole module exists to make loud."""
    logger.warning(
        "api_server quota gate: HELD %s turn (reason=%s session=%s walled=%s window=%s reset_at=%s)",
        door, decision.reason, session_id or "?", decision.walled_provider or "?",
        decision.window or "?", decision.reset_at,
    )
