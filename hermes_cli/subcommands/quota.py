"""``hermes quota`` subcommand parser (see ``hermes_cli/quota.py`` for handlers)."""

from __future__ import annotations

from typing import Callable


def build_quota_parser(subparsers, *, cmd_quota: Callable) -> None:
    """Attach the ``quota`` subcommand to ``subparsers``."""
    p = subparsers.add_parser(
        "quota",
        help="Manage the provider-quota gate (close/open/status/probe/release-all)",
        description=(
            "The provider-quota gate holds queued kanban work, due cron jobs and inbound "
            "peer DMs while the "
            "primary model's subscription window is exhausted, instead of spawning "
            "workers that bounce off the same 429. State lives in a local SQLite DB "
            "(quota_state.db) per install. `release-all on` is the operator's switch to stop "
            "holding them and route each to the fallback instead."
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

    status_p = sp.add_parser("status", help="Print the current gate state",
                             description="With --dual, also compares the card against the local state DB "
                                         "(Phase 1A validation tool). With --json, emits the gate state "
                                         "as JSON for scripts.")
    status_p.add_argument("--dual", action="store_true",
                          help="Compare the board card against the local state DB and report divergence")
    status_p.add_argument("--json", action="store_true",
                          help="Emit the gate state as JSON (machine-readable)")

    probe = sp.add_parser("probe", help="Verify the window with ONE real call")
    probe.add_argument("--base-url", help="Override the primary API base URL")
    probe.add_argument("--api-key", help="Override the primary API key")
    probe.add_argument("--model", help="Model for the probe call (default: the profile's)")

    release = sp.add_parser(
        "release-all",
        help="Turn the release-all switch ON/OFF for the current wall (kanban + cron + peer DMs)",
        description=(
            "The gate's on/off switch for the work that OBEYS the gate — kanban cards, "
            "cron jobs and inbound peer DMs — stored on the gate card itself. `on` = gate "
            "OFF: ready cards, due cron jobs and peer messages are not held and each is "
            "routed to the fallback head (use when "
            "the reset is too far away to wait); released cards and jobs still wait for that "
            "fallback's off-peak discount hours, so the release buys you progress without "
            "buying the peak rate, while an inbound peer DM runs at once because the peer is "
            "blocked on the reply. `off` = gate ON: they wait for the reset "
            "unless they carry a manual `quota_override` (use when the reset is close). It "
            "rides the wall, so it is settable only while the gate is closed, and the next "
            "wall starts in HOLD mode."
        ),
    )
    release.add_argument(
        "state", choices=["on", "off"],
        help="on = gate OFF (release ready cards, due jobs and peer DMs), off = hold again",
    )

    p.set_defaults(func=cmd_quota)
