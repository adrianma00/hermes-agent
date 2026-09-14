"""Provider-quota gate for cron: hold due jobs while the primary model's subscription
window is closed, and route override jobs to the fallback chain for that run only.

Sibling of ``cron.scheduler`` (late-binds it as ``_sched``), mirroring the kanban
dispatcher's gate so ONE shared gate card governs both work sources — a single
`quota-gate:<provider>` card on the shared board, read by every profile.

Split out of ``cron.scheduler`` per the facade+siblings rule (new behaviour goes in a
topical sibling, never appended to the loop module).
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


def _config_and_chain() -> Tuple[dict, list]:
    """``(cfg, fallback_chain)`` from the user config; ``({}, [])`` on any error.

    Fails open on a config read error: a broken config must not hold the fleet.
    """
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.fallback_config import get_fallback_chain

        cfg = load_config_readonly() or {}
        if not isinstance(cfg, dict):
            return {}, []
        return cfg, get_fallback_chain(cfg)
    except Exception:
        logger.debug("cron quota gate: config/fallback read failed — not gating", exc_info=True)
        return {}, []


def gate_state():
    """The shared quota gate, or ``None`` when the gate is disabled or open.

    ``None`` means "do not gate": the caller skips every quota check, so a fleet
    that never enabled the gate behaves exactly as before.
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


def job_is_exempt(job: dict, cfg: dict, gate) -> bool:
    """Whether this job sidesteps the gate: pinned to a provider other than the walled one.

    A job pinned to a healthy provider spends none of the walled quota, so holding
    it would be wrong. A job pinned TO the walled provider still waits.
    """
    return gate.is_exempt(_job_effective_provider(job, cfg))


def override_route(job: dict, gate) -> Optional[Tuple[str, str]]:
    """``(provider, model)`` for a ``quota_override`` job running while the gate is closed.

    ``None`` when the job carries no override or no fallback route is configured —
    the caller then holds the job like any other (running it would just bounce off
    the wall).
    """
    if not job.get("quota_override"):
        return None
    provider = str(gate.fallback_provider or "").strip()
    model = str(gate.fallback_model or "").strip()
    if not provider or not model:
        return None
    return provider, model


def hold_jobs(due_jobs: list, gate, *, now=None) -> Tuple[list, list]:
    """Split ``due_jobs`` into ``(fire, held)`` for one tick and re-arm the held ones.

    A held job is NOT executed and NOT advanced: its ``next_run_at`` is re-armed to the
    gate's ``reset_at`` so it runs as soon as the provider's window reopens. Re-arming
    (rather than leaving ``next_run_at`` in the past) matters because a job overdue past
    the catch-up window would otherwise be skipped permanently once the gate opens.

    An ``override`` job is exempt from holding — the caller routes it to the fallback
    chain for that run.
    """
    from datetime import datetime, timedelta

    from cron import scheduler as _sched
    from cron.jobs import update_job

    cfg, _chain = _config_and_chain()
    fire: list = []
    held: list = []
    reset_at = gate.reset_at

    for job in due_jobs:
        if job_is_exempt(job, cfg, gate):
            fire.append(job)
            continue
        if override_route(job, gate) is not None:
            fire.append(job)  # routed to the fallback chain by the caller
            continue
        held.append(job)

    if not held:
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
    return fire, held


def log_hold_summary(held: list, gate, exc: Optional[Exception] = None) -> None:
    """One line per tick summarising the hold — avoids per-job log spam on 60s ticks."""
    if not held:
        return
    logger.info(
        "Provider-quota gate: holding %d cron job(s) until the %s window reopens (%s)",
        len(held), gate.walled_provider or "primary",
        "reset known" if gate.reset_at else "reset unknown",
    )
