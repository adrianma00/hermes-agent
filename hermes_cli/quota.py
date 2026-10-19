"""``hermes quota`` — manage the shared provider-quota gate card.

CLI subcommand registered via ``_forward_command`` in main.py.  See
``hermes_cli.kanban_db`` for the gate card reader/writer and
``cron.scheduler_quota`` for the cron-level enforcement.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _kb():
    """Lazy import to keep module-level imports cheap."""
    from hermes_cli import kanban_db as _m
    return _m


def quota_command(args: argparse.Namespace) -> int:
    """Dispatch to the right handler based on the first positional argument."""
    action = args.action
    if action == "close":
        return _cmd_close(args)
    if action == "open":
        return _cmd_open(args)
    if action == "status":
        return _cmd_status(args)
    if action == "probe":
        return _cmd_probe(args)
    if action == "release-all":
        return _cmd_release_all(args)
    return _err(f"unknown quota action: {action!r}")


def _err(msg: str, rc: int = 1) -> int:
    print(f"quota: {msg}", file=sys.stderr)
    return rc


def _cmd_close(args: argparse.Namespace) -> int:
    """Close the quota gate for a provider.

    Writes or updates the gate card on the shared board.  The card body
    carries the provider identity, the window kind, the close instant, and
    the reset instant from the provider's own error message.
    """
    provider = args.provider
    reset_at = args.reset_at
    window = args.window or "5h"

    if not provider:
        return _err("provider is required")
    if not reset_at:
        return _err("--reset-at is required (epoch seconds)")

    kb = _kb()
    gate_state = kb.read_quota_gate_state(enabled=True)
    if gate_state.is_closed and gate_state.reset_at and gate_state.reset_at > reset_at:
        # A tighter (longer) reset is already in effect; don't backslide.
        print(f"Gate already closed for {provider} until "
              f"{time.strftime('%H:%M:%S', time.localtime(gate_state.reset_at))} "
              f"(asked to close until {time.strftime('%H:%M:%S', time.localtime(reset_at))})")
        return 0

    # Phase 2A: write to the local state DB only.
    try:
        from hermes_cli.quota_state import write_wall as _write_wall_local
        _write_wall_local(provider, window=window, reset_at=float(reset_at), source="manual-close")
    except Exception:
        pass

    print(f"Gate CLOSED for {provider} "
          f"until {time.strftime('%H:%M:%S', time.localtime(reset_at))} "
          f"(window: {window})")
    return 0


def _cmd_open(args: argparse.Namespace) -> int:
    """Open the quota gate — remove the gate card (if any) from the shared board."""
    provider = args.provider
    if not provider:
        return _err("provider is required")
    kb = _kb()

    # Phase 2A: delete from the local state DB only.
    try:
        from hermes_cli.quota_state import delete_wall as _delete_wall_local
        local_removed = _delete_wall_local(provider)
    except Exception:
        local_removed = False

    if local_removed:
        print(f"Gate OPEN for {provider}")
    else:
        print(f"Gate was already open for {provider}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Print the current gate state."""
    kb = _kb()
    # The chain MUST ride along: without it ``state.fallback_model`` is always None, so a closed
    # gate printed "Hard pause: no fallback route" on every install that has one while the
    # dispatcher was happily routing ``quota_override`` cards to it.
    state = kb.read_quota_gate_state(kb.quota_gate_fallback_chain(), enabled=True)

    # Phase 1A validation: --dual compares card vs local state DB
    dual = getattr(args, "dual", False)
    if dual:
        _print_dual_status(state, kb)

    # Phase 2C: machine-readable output for scripts (e.g. provider_balance_watch's
    # ownership check, which can no longer read the retired board card).
    if getattr(args, "json", False):
        try:
            from hermes_cli.quota_state import list_walls as _list_walls
            walls = _list_walls()
        except Exception:
            walls = []
        print(json.dumps({
            "is_closed": bool(state.is_closed),
            "walled_provider": state.walled_provider,
            "window": state.window,
            "reset_at": state.reset_at,
            "release_all": bool(state.release_all),
            "fallback_provider": state.fallback_provider,
            "fallback_model": state.fallback_model,
            "walls": walls,
        }))
        return 0

    if state.is_closed:
        walled = state.walled_provider or "unknown"
        win = f" [{state.window} window]" if state.window else ""
        if state.reset_at:
            # Show the DATE, not just a clock time: "reset at (23:59:59)" is how a
            # 13-day monthly wall reads as "tonight", which is the single most
            # misleading thing this command used to print.
            when = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(state.reset_at))
            left = state.reset_at - time.time()
            if left >= 86400:
                rel = f"{left / 86400:.1f} days"
            elif left >= 3600:
                rel = f"{left / 3600:.1f} hours"
            else:
                rel = f"{max(1, int(left // 60))} min"
            print(f"Gate CLOSED for {walled}{win} — resets {when} (in {rel})")
        else:
            # No reset time = nothing will reopen this automatically. Say so.
            print(f"Gate CLOSED for {walled}{win} — reset time UNKNOWN; will NOT auto-reopen.")
            print("  Fix: `hermes quota open <provider>` (or set reset_at on the gate card).")
        if state.fallback_model:
            print(f"  Override route: {state.fallback_provider}:{state.fallback_model}")
        else:
            print("  Hard pause: no fallback route — cards wait for the reset.")
        # The switch is the difference between "cards are waiting" and "cards are
        # being routed", so the status line has to name it: with release-all on, a
        # board with zero `quota_paused` rows is healthy, not a dead dispatcher.
        if state.release_all:
            # Cards and jobs are QUEUED work, so releasing them buys progress without buying
            # the peak rate: they still wait for the metered fallback's off-peak hours. A peer
            # DM is interactive — someone else's next step is blocked on the reply (R37) — so
            # there is nothing to defer and it is routed immediately, discount window or not.
            print("  Mode: RELEASE-ALL (gate OFF) — ready cards, due cron jobs and inbound "
                  "peer DMs are NOT "
                  "held; each is routed to the fallback head. Cards and jobs still wait for "
                  "that provider's off-peak hours; an inbound peer DM runs at once. "
                  "`hermes quota release-all off` "
                  "to hold again.")
        else:
            print("  Mode: HOLD (gate ON) — ready cards and due cron jobs wait for the "
                  "reset unless they carry `quota_override`; inbound peer DMs are refused "
                  "with the reset time instead of being run.")
    else:
        print("Gate OPEN — all providers available")
    return 0


def _print_dual_status(card_state, kb) -> None:
    """Compare the board card against the local state DB and report divergence."""
    try:
        from hermes_cli.quota_state import read_state as _read_local, list_walls as _list_local
    except Exception as exc:
        print(f"  [--dual] Local state DB unavailable: {exc}")
        return

    local_state = _read_local(kb.quota_gate_fallback_chain())
    local_walls = _list_local()

    print("─── Dual-read validation ───")
    print(f"  Board card:  {'CLOSED' if card_state.is_closed else 'OPEN'}"
          f"{' (release_all)' if card_state.release_all else ''}")
    print(f"  Local DB:    {'CLOSED' if local_state.is_closed else 'OPEN'}"
          f"{' (release_all)' if local_state.release_all else ''}")

    if local_walls:
        for w in local_walls:
            r = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(w["reset_at"]))
            print(f"    Provider={w['provider']}  window={w['window']}  "
                  f"reset={r}  source={w['source']}  "
                  f"release_all={'YES' if w['release_all'] else 'no'}")
    else:
        print("    (no walls in local DB)")

    if card_state.is_closed != local_state.is_closed:
        print("  ⚠ DIVERGENCE: gate open/closed disagrees between card and local DB")
    if card_state.release_all != local_state.release_all:
        print("  ⚠ DIVERGENCE: release_all disagrees between card and local DB")
    if card_state.walled_provider != local_state.walled_provider:
        print(f"  ⚠ DIVERGENCE: walled provider differs "
              f"(card={card_state.walled_provider!r}, local={local_state.walled_provider!r})")
    if not card_state.is_closed and not local_state.is_closed:
        print("  ✓ OK — both report gate open")
    elif card_state.is_closed and local_state.is_closed:
        print("  ✓ OK — both report gate closed")
    print("──────────────────────────")


def _cmd_release_all(args: argparse.Namespace) -> int:
    """Turn the gate's release-all switch ON/OFF for the wall that is currently closed.

    The switch is the operator's answer to "is it worth waiting out this reset?", and it
    governs ALL the work that obeys the gate — kanban cards, cron jobs, and inbound peer
    DMs (``api_server`` turns):

      * ``on``  — gate OFF: ready cards, due model-calling cron jobs and peer messages are
                  no longer held; each is routed to the fallback head (cards and jobs still
                  wait for the cost window's discount hours; a peer DM runs at once, since
                  the peer is blocked on the reply).
      * ``off`` — gate ON: ready cards and due jobs wait for the reset again, unless they
                  carry ``quota_override``; a peer message is refused with the reset time
                  instead of being run, because there is no queue on either side of a DM.

    It rides the gate card, so it exists only while a wall does: with no card
    (gate open) there is nothing to switch and the command fails rather than
    reporting a success it did not achieve.
    """
    enabled = args.state == "on"
    kb = _kb()

    # Phase 2A: read gate state to find the walled provider, then set release_all locally.
    state = kb.read_quota_gate_state(enabled=True)
    if not state.is_closed:
        return _err(
            "The gate is OPEN — there is nothing to release. "
            "The switch rides the wall: set it once a wall is closed."
        )

    walled = state.walled_provider or "the primary"
    try:
        from hermes_cli.quota_state import set_release_all as _set_release_all_local
        _set_release_all_local(walled, enabled)
    except Exception:
        pass

    reset_at = state.reset_at
    until = (
        f" until {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(reset_at))}"
        if isinstance(reset_at, (int, float)) else ""
    )
    if not enabled:
        print(f"Gate mode: ON — {walled} walled{until}; ready cards and due cron jobs wait "
              "for the reset unless they carry `quota_override` (inbound peer DMs are "
              "refused with the reset time)")
        return 0

    chain = kb.quota_gate_fallback_chain()
    print(f"Gate mode: OFF — {walled} walled{until}; ready cards, due cron jobs and inbound "
          "peer DMs are no "
          "longer held and each is routed to the fallback head")
    if chain:
        head = chain[0]
        print(f"  Route: {head.get('provider')}:{head.get('model')}")
        # Released work is routed to a METERED provider, so the price still applies: the
        # release authorises the spend, it does not license the peak rate. The one exception
        # is an inbound peer DM: nobody queues it, and the peer is blocked on the reply.
        print("  Note: released cards and cron jobs still wait for that provider's off-peak "
              "(discount) hours — `hermes cost-window status` to see the window. A card's "
              "`run_now`, or a job's `run_now`, forces it through at peak price. An inbound "
              "peer DM is routed immediately: the sending bot is blocked on the reply.")
    else:
        print("  WARNING: no fallback route is configured — cards and jobs will still be "
              "held (an authorisation with nowhere to route pauses like everything else). "
              "Check `fallback_providers`.")
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    """Verify the primary provider's quota window is open by making ONE call.
    On 429 (still walled): re-closes the gate with the new reset time from
    the error body and returns 1.
    On other errors: prints the error and returns 1.
    """
    kb = _kb()
    state = kb.read_quota_gate_state(enabled=True)
    if not state.is_closed:
        print("Gate is already open — no probe needed")
        return 0

    provider = state.walled_provider or "custom"
    base_url = getattr(args, "base_url", None)
    api_key = getattr(args, "api_key", None)
    model = getattr(args, "model", None)

    # If no explicit credentials, try reading from the current profile's config.
    if not base_url or not api_key or not model:
        try:
            from hermes_cli.config import load_config_readonly
            cfg = load_config_readonly() or {}
            mc = cfg.get("model") or {}
            base_url = base_url or mc.get("base_url", "")
            api_key = api_key or mc.get("api_key", "")
            model = model or mc.get("default", "")
        except Exception:
            pass

    if not base_url or not api_key:
        return _err("Cannot resolve primary provider endpoint. "
                    "Set --base-url and --api-key or configure model in config.yaml")
    if not model:
        return _err("Cannot resolve the primary model. Set --model or configure model.default")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "probe"}],
        "max_tokens": 1,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    import urllib.request

    url = base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        if resp.status == 200:
            from hermes_cli.quota_state import delete_wall as _delete_wall_local
            _delete_wall_local(provider)
            print(f"Gate OPEN for {provider} — window is available")
            return 0
        return _err(f"Unexpected HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        resp_body = exc.read().decode() if exc.fp else ""
        if exc.code == 429:
            # Re-close with the new reset time from the response body.
            new_reset = _extract_reset_from_429(resp_body)
            if new_reset:
                from hermes_cli.quota_state import write_wall as _write_wall_local
                _write_wall_local(provider, window="5h", reset_at=float(new_reset), source="probe-429")
                print(f"Gate still CLOSED for {provider} — "
                      f"re-armed to {time.strftime('%H:%M:%S', time.localtime(new_reset))}")
            else:
                print(f"Gate still CLOSED for {provider} (HTTP 429, no reset time in response)")
            return 1
        return _err(f"Probe failed: HTTP {exc.code}: {resp_body[:200]}")
    except Exception as exc:
        return _err(f"Probe failed: {exc}")


def _extract_reset_from_429(body_text: str) -> Optional[float]:
    """Extract the reset timestamp from a 429 error body, same format as
    the Ark ``AccountQuotaExceeded`` response."""
    try:
        body = json.loads(body_text)
        msg = ((body.get("error") or {}) if isinstance(body, dict) else {}).get("message", "")
        if not isinstance(msg, str):
            return None
    except (json.JSONDecodeError, AttributeError):
        msg = body_text

    from agent.credential_pool import _extract_reset_at_from_message
    return _extract_reset_at_from_message(msg)
