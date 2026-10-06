"""``hermes quota`` subcommand parser (see ``hermes_cli/quota.py`` for handlers)."""

from __future__ import annotations

from typing import Callable


def build_quota_parser(subparsers, *, cmd_quota: Callable) -> None:
    """Attach the ``quota`` subcommand to ``subparsers``."""
    p = subparsers.add_parser(
        "quota",
        help="Manage the provider-quota gate (close/open/status/probe/release-all)",
        description=(
            "The provider-quota gate holds queued kanban work and cron jobs while the "
            "primary model's subscription window is exhausted, instead of spawning "
            "workers that bounce off the same 429. State lives in one "
            "'quota-gate:<provider>' card on the shared board, so every profile in the "
            "fleet reads the same gate. `release-all on` is the operator's switch to stop "
            "holding KANBAN cards and route them to the fallback instead."
        ),
    )
    sp = p.add_subparsers(dest="action", required=True)

    close = sp.add_parser("close", help="Close the gate for a provider")
    close.add_argument("provider", help="Provider identity (e.g. 'custom')")
    close.add_argument("--reset-at", type=int, required=True,
                       help="Epoch seconds when the quota window reopens")
    close.add_argument("--window", default="5h",
                       help="Quota window label recorded on the card (default: 5h)")

    open_p = sp.add_parser("open", help="Open the gate (removes the card)")
    open_p.add_argument("provider", help="Provider identity (e.g. 'custom')")

    sp.add_parser("status", help="Print the current gate state")

    probe = sp.add_parser("probe", help="Verify the window with ONE real call")
    probe.add_argument("--base-url", help="Override the primary API base URL")
    probe.add_argument("--api-key", help="Override the primary API key")
    probe.add_argument("--model", help="Model for the probe call (default: the profile's)")

    release = sp.add_parser(
        "release-all",
        help="Turn the release-all switch ON/OFF for the current wall (kanban + cron)",
        description=(
            "The gate's on/off switch for the work that OBEYS the gate — kanban cards and "
            "cron jobs — stored on the gate card itself. `on` = gate OFF: ready cards and "
            "due cron jobs are not held and each is routed to the fallback head (use when "
            "the reset is too far away to wait). `off` = gate ON: they wait for the reset "
            "unless they carry a manual `quota_override` (use when the reset is close). It "
            "rides the wall, so it is settable only while the gate is closed, and the next "
            "wall starts in HOLD mode."
        ),
    )
    release.add_argument(
        "state", choices=["on", "off"],
        help="on = gate OFF (release ready cards and due jobs), off = hold again",
    )

    p.set_defaults(func=cmd_quota)
