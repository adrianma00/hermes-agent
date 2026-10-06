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

    body = {
        "v": 1,
        "provider": provider,
        "window": window,
        "closed_at": int(time.time()),
        "reset_at": int(reset_at),
        "opened_at": None,
    }
    kb.write_quota_gate_card(provider, body, board=kb.quota_gate_board())
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
    removed = kb.delete_quota_gate_card(provider, board=kb.quota_gate_board())
    if removed:
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
            print("  Mode: RELEASE-ALL (gate OFF for kanban) — ready cards are NOT held; "
                  "each spawn routes to the fallback head. `hermes quota release-all off` "
                  "to hold again.")
        else:
            print("  Mode: HOLD (gate ON for kanban) — ready cards wait for the reset "
                  "unless they carry `quota_override`.")
    else:
        print("Gate OPEN — all providers available")
    return 0


def _cmd_release_all(args: argparse.Namespace) -> int:
    """Turn the gate's release-all switch ON/OFF for the wall that is currently closed.

    The switch is the operator's answer to "is it worth waiting out this reset?":

      * ``on``  — gate OFF for kanban: ready cards are no longer held; each spawn is
                  routed to the fallback head (the cost window still defers them to
                  that provider's discount hours, unchanged).
      * ``off`` — gate ON for kanban: ready cards wait for the reset again, unless
                  they carry ``quota_override``.

    It rides the gate card, so it exists only while a wall does: with no card
    (gate open) there is nothing to switch and the command fails rather than
    reporting a success it did not achieve.
    """
    enabled = args.state == "on"
    kb = _kb()
    result = kb.set_quota_gate_release_all(enabled)
    if not result.get("ok"):
        if result.get("reason") == "no_gate_card":
            return _err(
                f"No gate card on {result.get('board')} — the gate is OPEN, so there is "
                "nothing to release. The switch rides the wall: set it once a wall is closed."
            )
        return _err(f"Cannot reach the quota-gate board {result.get('board')!r}")

    walled = result.get("provider") or "the primary"
    reset_at = result.get("reset_at")
    until = (
        f" until {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(reset_at))}"
        if isinstance(reset_at, (int, float)) else ""
    )
    if not enabled:
        print(f"Gate mode: ON for kanban — {walled} walled{until}; ready cards wait for "
              "the reset unless they carry `quota_override`")
        return 0

    chain = kb.quota_gate_fallback_chain()
    print(f"Gate mode: OFF for kanban — {walled} walled{until}; ready cards are no longer "
          "held and each spawn is routed to the fallback head")
    if chain:
        head = chain[0]
        print(f"  Route: {head.get('provider')}:{head.get('model')}")
    else:
        print("  WARNING: no fallback route is configured — cards will still be held "
              "(an authorisation with nowhere to route pauses like everything else). "
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
            kb.delete_quota_gate_card(provider)
            print(f"Gate OPEN for {provider} — window is available")
            return 0
        return _err(f"Unexpected HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        resp_body = exc.read().decode() if exc.fp else ""
        if exc.code == 429:
            # Re-close with the new reset time from the response body.
            new_reset = _extract_reset_from_429(resp_body)
            if new_reset:
                kb.write_quota_gate_card(provider, {
                    "v": 1, "provider": provider, "window": "5h",
                    "closed_at": int(time.time()), "reset_at": new_reset, "opened_at": None,
                }, board=kb.quota_gate_board())
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
