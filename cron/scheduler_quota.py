"""Provider-quota gate for cron: hold due jobs while the primary model's subscription
window is closed, and route override jobs to the fallback chain for that run only.

Sibling of ``cron.scheduler`` (late-binds it as ``_sched``), mirroring the kanban
dispatcher's gate so ONE shared gate card governs both work sources — a single
`quota-gate:<provider>` card on the shared board, read by every profile.

Two authorisations let a job through the closed gate, and both are read off that card:
a per-job ``quota_override`` flag, and the fleet-wide ``release_all`` switch the operator
sets on the card itself (``hermes quota release-all on``). The switch is the same field
the kanban dispatcher honours, so one flip covers kanban cards AND cron jobs.

A job the gate releases is then subject to the SAME provider cost-window deferral the
kanban dispatcher applies (``kanban_cost_window``): releasing a job from the quota wall
routes it to the metered fallback, and running that fallback inside its peak-price hours
costs double for no benefit on work that is not latency-critical. So a released job waits
for the fallback's off-peak window exactly as a released card does — the two work sources
that share this gate must not disagree about when the discounted hours are. ``run_now`` on
the job forces it immediately, mirroring the card flag.

Split out of ``cron.scheduler`` per the facade+siblings rule (new behaviour goes in a
topical sibling, never appended to the loop module).
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


def _config_and_chain() -> Tuple[dict, list]:
    """``(cfg, fallback_chain)`` from the user config; ``({}, [])`` on any error.

    The chain comes from the shared ``kanban_db.quota_gate_fallback_chain`` resolver — the
    same one the kanban dispatcher and ``hermes quota status`` use — so the three surfaces
    cannot disagree about the override route.  Fails open on a config read error: a broken
    config must not hold the fleet.
    """
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.kanban_db import quota_gate_fallback_chain

        cfg = load_config_readonly() or {}
        if not isinstance(cfg, dict):
            return {}, []
        return cfg, quota_gate_fallback_chain() or []
    except Exception:
        logger.debug("cron quota gate: config/fallback read failed — not gating", exc_info=True)
        return {}, []


def gate_state():
    """The shared quota gate, or ``None`` when the gate is disabled or open.

    ``None`` means "do not gate": the caller skips every quota check, so a fleet
    that never enabled the gate behaves exactly as before.

    Phase 2A: reads from the local state DB; no sweep is needed because expired
    walls are automatically filtered by the reader (``reset_at > now``).
    """
    try:
        from hermes_cli.kanban_db import read_quota_gate_state
    except Exception:
        return None
    cfg, chain = _config_and_chain()
    try:
        state = read_quota_gate_state(chain)
    except Exception:
        logger.debug("cron quota gate: state read failed — not gating", exc_info=True)
        return None
    return state if state.is_closed else None


def _job_effective_provider(job: dict, cfg: dict) -> str:
    """The provider this job would run on, following run_job's own precedence.

    Per-job pin > ``cron.model_provider`` > creation snapshot > global
    ``model.provider``. Mirrors ``_resolve_job_runtime`` so a job that the gate
    would HOLD is the same job that would spend the walled quota.
    """
    pinned = str(job.get("provider") or "").strip()
    if pinned:
        return pinned
    cron_cfg = cfg.get("cron") if isinstance(cfg.get("cron"), dict) else {}
    default_provider = str((cron_cfg or {}).get("model_provider") or "").strip()
    if default_provider:
        return default_provider
    snapshot = str(job.get("provider_snapshot") or "").strip()
    if snapshot:
        return snapshot
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    return str((model_cfg or {}).get("provider") or "").strip()


def job_needs_model(job: dict) -> bool:
    """Whether firing this job will make a model call (and so spend walled quota).

    A ``no_agent`` job IS its script: it runs, delivers stdout, and never touches a
    provider. Holding one therefore buys nothing and costs the fleet its monitoring
    for the whole pause — and a quota pause is precisely when an unattended fleet
    needs its monitors most. Hit for real: during the 2026-09 wall the Vault came up
    sealed after a host reboot and the ``no_agent`` health check that would have
    caught it was itself held until the reset, i.e. blind for ~13 days.
    """
    return not job.get("no_agent")


def job_is_exempt(job: dict, cfg: dict, gate) -> bool:
    """Whether this job sidesteps the gate: pinned to a provider other than the walled one.

    A job pinned to a healthy provider spends none of the walled quota, so holding
    it would be wrong. A job pinned TO the walled provider still waits.
    """
    return gate.is_exempt(_job_effective_provider(job, cfg))


def release_all(gate) -> bool:
    """Whether the operator's fleet-wide release-all switch is on the gate card.

    The SAME card field the kanban dispatcher reads (``QuotaGateState.release_all``), so
    one flip covers both work sources. ``getattr`` by design: the gate may be a test
    double or a state object predating the field, and an unknown switch value must read as
    HOLD, never as "release everything".
    """
    return bool(getattr(gate, "release_all", False))


def override_route(job: dict, gate) -> Optional[Tuple[str, str]]:
    """``(provider, model)`` for a job authorised to run while the gate is closed.

    Authorised means EITHER the job carries ``quota_override`` (the per-job lever) OR the
    fleet-wide ``release_all`` switch is on the gate card — the same flag the kanban
    dispatcher honours. ``None`` when neither applies, or when no fallback route is
    configured: the caller then holds the job like any other, because running it would
    just bounce off the wall.
    """
    if not job.get("quota_override") and not release_all(gate):
        return None
    provider = str(gate.fallback_provider or "").strip()
    model = str(gate.fallback_model or "").strip()
    if not provider or not model:
        return None
    return provider, model


def _as_epoch(now) -> Optional[float]:
    """``now`` as a float epoch, or ``None``. Accepts an epoch, a ``datetime``, or None.

    ``hold_jobs`` threads ``now`` straight through from its caller, and it is a plain
    injection point — so the value can arrive as either shape. The cost-window reader takes
    an epoch only, and a ``TypeError`` raised there would be swallowed by the fail-open
    handler and silently disable the deferral for that tick, which is exactly the wrong
    failure mode for a pricing decision.
    """
    if now is None:
        return None
    if isinstance(now, bool):
        return None
    if isinstance(now, (int, float)):
        return float(now)
    to_epoch = getattr(now, "timestamp", None)
    if callable(to_epoch):
        try:
            return float(to_epoch())
        except (TypeError, ValueError, OSError):
            return None
    return None


def released_route_provider(gate) -> Optional[str]:
    """The provider a gate-released job will actually RUN on, for the cost window.

    The fire path (``_process_due_job``) routes a released job by OVERWRITING its
    provider/model with the gate's fallback head — a job pinned to the WALLED provider is
    released and rerouted like every other, while a job pinned to a DIFFERENT provider never
    reaches this branch at all (``job_is_exempt`` fires it untouched, spending none of the
    walled quota). So for anything that gets here the answer is the fallback head, and this
    must ask about the fallback's discount hours — not the walled provider's, whose flat-rate
    plan has no time-of-day price at all.

    ``None`` when the gate carries no fallback route: the caller then treats the route as
    priced, matching ``CostWindowState.covers(None)``.
    """
    fallback = str(getattr(gate, "fallback_provider", "") or "").strip()
    if fallback:
        return fallback
    return None


def cost_window_deferral(job: dict, gate, *, now: Optional[float] = None):
    """``CostWindowState`` for a gate-released job, or ``None`` when it may run now.

    The cron mirror of the dispatcher's cost-window block: a job the gate released is
    routed to the metered fallback, and the discount for that provider is a time-of-day
    question the gate card says nothing about. Releasing a job must therefore defer it
    into the fallback's off-peak hours rather than buying the same tokens at double
    price — the release is an authorisation to SPEND the fallback, not a licence to pay
    peak for it.

    Two deliberate refusals, both matching the dispatcher:

    * a **script job** (``no_agent``) makes no model call, so there is nothing to price
      and nothing to wait for; and
    * a job carrying ``run_now`` is the operator (or the job's own definition) asking for
      it immediately, exactly as the per-card flag does on the board.

    The reader FAILS OPEN (see ``read_cost_window_state``), so a clock or config problem
    returns a non-deferring state and the job runs — the cost of guessing wrong here is
    money, never a stalled fleet.
    """
    if not job_needs_model(job):
        return None
    if job.get("run_now"):
        return None
    try:
        from hermes_cli.kanban_cost_window import read_cost_window_state
    except Exception:
        logger.debug("cron cost window: reader unavailable — not deferring", exc_info=True)
        return None
    try:
        # The reader takes an EPOCH (``datetime.fromtimestamp(now)``); ``hold_jobs`` exposes
        # ``now`` as an injection point for tests, so normalise anything datetime-shaped
        # rather than letting a TypeError escape into the tick.
        epoch = _as_epoch(now)
        state = read_cost_window_state(now=epoch)
    except Exception:
        logger.debug("cron cost window: state read failed — not deferring", exc_info=True)
        return None
    if not state.is_deferred:
        return None
    if not state.covers(released_route_provider(gate)):
        return None
    return state


def hold_jobs(due_jobs: list, gate, *, now=None) -> Tuple[list, list]:
    """Split ``due_jobs`` into ``(fire, held)`` for one tick and re-arm the held ones.

    A held job is NOT executed and NOT advanced: its ``next_run_at`` is re-armed to the
    gate's ``reset_at`` so it runs as soon as the provider's window reopens. Re-arming
    (rather than leaving ``next_run_at`` in the past) matters because a job overdue past
    the catch-up window would otherwise be skipped permanently once the gate opens.

    An ``override`` job is exempt from holding — the caller routes it to the fallback
    chain for that run. So is ANY model-calling job while the fleet ``release_all`` switch
    is on the card: the switch is the operator saying "this reset is too far away to wait
    for, spend the fallback instead".

    A job the switch releases is then checked against the provider COST window, because
    its route is a metered fallback: if that provider is inside its peak-price hours the
    job is held again — this time until the discount window opens rather than until the
    quota reset. Releasing a job authorises spending the fallback; it must not license
    paying double for the same tokens. Such a job is re-armed to ``resume_at`` from the
    cost window, so the two holds compose (quota reset first, then off-peak).
    """
    from datetime import datetime, timedelta

    from cron import scheduler as _sched
    from cron.jobs import update_job

    cfg, _chain = _config_and_chain()
    fire: list = []
    held: list = []
    reset_at = gate.reset_at
    # ``(job, resume_epoch, why)`` for jobs held by the COST window rather than the quota
    # reset, so each is re-armed to the instant that actually unblocks it.
    peak_held: list = []

    for job in due_jobs:
        if not job_needs_model(job):
            # Pure script: spends none of the walled quota, so it always fires.
            fire.append(job)
            continue
        if job_is_exempt(job, cfg, gate):
            fire.append(job)
            continue
        if override_route(job, gate) is not None:
            # Authorised through the gate — but the route is a metered fallback, so the
            # cost window still decides WHEN. A peak-hours deferral here is the whole
            # point of this branch: releasing must not mean paying double.
            cost_state = cost_window_deferral(job, gate, now=now)
            if cost_state is not None:
                peak_held.append((job, cost_state.resume_at, cost_state.reason))
                continue
            fire.append(job)  # routed to the fallback chain by the caller
            continue
        held.append(job)

    if not held and not peak_held:
        return fire, held

    # One hour past the reset: a little slack so the window is genuinely open when the
    # job next comes due (the 5h window is rolling, so the quoted instant is the earliest
    # moment a call is admitted, not a guarantee of headroom).
    target_dt = None
    if isinstance(reset_at, (int, float)):
        target_dt = datetime.fromtimestamp(float(reset_at)) + timedelta(minutes=1)
    target_iso = target_dt.isoformat() if target_dt is not None else None

    for job in held:
        job_id = str(job.get("id") or "")
        try:
            updates: dict[str, Any] = {}
            if target_iso:
                updates["next_run_at"] = target_iso
            updates["last_error"] = (
                f"held by the provider-quota gate until {target_iso or 'the window reopens'}"
            )
            if job_id:
                update_job(job_id, updates)
        except Exception:
            logger.warning(
                "cron quota gate: could not re-arm job %s — it stays due and will be "
                "held again next tick", job_id, exc_info=True,
            )
        logger.info(
            "Job '%s' held by the provider-quota gate (%s walled until %s)",
            job.get("name") or job_id, gate.walled_provider or "primary",
            target_iso or "unknown",
        )

    # Jobs held by the COST window are re-armed to the discount window's own end, not the
    # quota reset: the release already authorised the spend, so the only thing left to wait
    # for is the price. Kept separate from the quota re-arm above because the two instants
    # are independent — a job can be off-peak long before the quota resets, and re-arming
    # it to the reset would waste the discount hours it was released to use.
    for job, resume_at, why in peak_held:
        job_id = str(job.get("id") or "")
        peak_iso = None
        if isinstance(resume_at, (int, float)):
            peak_iso = datetime.fromtimestamp(float(resume_at)).isoformat()
        try:
            updates: dict[str, Any] = {}
            if peak_iso:
                updates["next_run_at"] = peak_iso
            updates["last_error"] = (
                f"released by the quota switch but held for the fallback's off-peak "
                f"window until {peak_iso or 'the discount hours open'}"
            )
            if job_id:
                update_job(job_id, updates)
        except Exception:
            logger.warning(
                "cron cost window: could not re-arm released job %s — it stays due and "
                "will be deferred again next tick", job_id, exc_info=True,
            )
        logger.info(
            "Job '%s' released by the quota switch but held for the fallback's off-peak "
            "window (%s, until %s)", job.get("name") or job_id, why or "peak_hours",
            peak_iso or "unknown",
        )

    # A peak-deferred job is still a job that did NOT fire, so it belongs in what the
    # caller reads as "held" — a caller that only looks at `held` must not conclude the
    # fleet ran work it actually deferred. It was re-armed above to the cost window's own
    # end, so it is appended AFTER the quota re-arm loop and never re-armed twice.
    held.extend(job for job, _resume, _why in peak_held)
    return fire, held


def log_hold_summary(held: list, gate, exc: Optional[Exception] = None) -> None:
    """One line per tick summarising the hold — avoids per-job log spam on 60s ticks.

    Covers both reasons a job can now be held: the wall itself, and the fallback's peak
    price after the release-all switch authorised the spend. The per-job lines say which;
    this says how many, and that the reason is a deliberate gate rather than a stall.
    """
    if not held:
        return
    logger.info(
        "Provider-quota gate: holding %d cron job(s) until the %s window reopens (%s); "
        "jobs released onto a metered fallback are held for that provider's off-peak "
        "hours instead", len(held), gate.walled_provider or "primary",
        "reset known" if gate.reset_at else "reset unknown",
    )
