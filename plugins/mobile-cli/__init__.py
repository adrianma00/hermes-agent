"""Mobile pairing CLI plugin — ``hermes mobile``.

Reads the install's real configuration (profile config, ``.env``, tailscale),
shows what a mobile client needs, and renders the ``hermes://pair`` payload as a
QR code. Operator-facing only: no model tools are registered, and the key is
never printed (except with the explicit ``--link`` opt-in).
"""

from __future__ import annotations

from . import pairing, qrterm
from .cli import register_cli, mobile_command

__all__ = ["pairing", "qrterm", "register_cli", "mobile_command"]


def register(ctx) -> None:
    ctx.register_cli_command(
        name="mobile",
        help="Show mobile-pairing state for this install and render a pairing QR",
        setup_fn=register_cli,
        handler_fn=mobile_command,
        description=(
            "Mobile pairing helper. Reports the address, port, TLS state and "
            "Dashboard URL a phone client needs, then renders the hermes://pair "
            "payload as a QR code (or, with --link, as a link). The Gateway key is "
            "read from .env and never printed by default."
        ),
    )
