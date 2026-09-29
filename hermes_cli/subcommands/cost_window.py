"""``hermes cost-window`` subcommand parser (handlers in ``hermes_cli/cost_window.py``)."""

from __future__ import annotations

from typing import Callable


def build_cost_window_parser(subparsers, *, cmd_cost_window: Callable) -> None:
    """Attach the ``cost-window`` subcommand to ``subparsers``."""
    p = subparsers.add_parser(
        "cost-window",
        help="Manage the provider cost-window gate (defer dispatch to off-peak hours)",
        description=(
            "Some providers price by time of day: DeepSeek bills peak rates "
            "01:00-04:00 and 06:00-10:00 UTC, Mon-Fri, and off-peak at half price "
            "every other hour (incl. all weekend). The cost-window gate holds "
            "kanban dispatch out of the peak windows so batch work runs at the "
            "discounted rate, instead of spawning the instant a card is created. "
            "It is the twin of the provider-quota gate, with two deliberate "
            "differences: it FAILS OPEN (a clock/config problem must cost time, "
            "never stop the fleet) and it holds no cross-install state, because "
            "the boundary recurs daily and every install computes it from the "
            "clock."
        ),
    )
    sp = p.add_subparsers(dest="action", required=True)

    sp.add_parser("status", help="Print whether dispatch is currently deferred")

    open_p = sp.add_parser(
        "open",
        help="Bypass the window fleet-wide for a while (writes the shared card)",
    )
    open_p.add_argument(
        "for_",
        metavar="DURATION",
        nargs="?",
        help="How long to bypass: 2h, 90m, 45s, or 'indefinite'. Defaults to 2h.",
    )

    sp.add_parser(
        "close", help="Clear any fleet-wide bypass (resume normal windowing)"
    )

    p.set_defaults(func=cmd_cost_window)
