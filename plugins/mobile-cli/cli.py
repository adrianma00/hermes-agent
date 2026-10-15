"""``hermes mobile`` — instructions and a scannable pairing code for mobile clients.

Examples:

    hermes mobile                  # state + QR (key never printed)
    hermes mobile --link           # print the link instead of the QR
    hermes mobile --json           # machine-readable state (no key, no link)
    hermes mobile --label "Em"     # override the instance name
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import pairing, qrterm

_NEEDS = "Console 1.2.15 or newer on the phone. Guide: https://hermes.xpetal.dev"


def register_cli(subparser: "argparse.ArgumentParser") -> None:
    """Wire the argparse tree for ``hermes mobile``."""
    subparser.add_argument("--label", metavar="NAME",
                           help="instance name the app shows (default: this host's name)")
    subparser.add_argument("--host", metavar="ADDR",
                           help="address the phone uses (default: config.yaml)")
    subparser.add_argument("--port", type=int, metavar="PORT",
                           help="gateway port (default: config.yaml)")
    subparser.add_argument("--dashboard", metavar="URL",
                           help="Dashboard URL to embed (default: dashboard.public_url)")
    subparser.add_argument("--dash-user", metavar="USER",
                           help="Dashboard username to embed")
    subparser.add_argument("--dash-pass-stdin", action="store_true",
                           help="read the Dashboard password from stdin (never argv)")
    subparser.add_argument("--link", action="store_true",
                           help="print the pairing link instead of the QR code")
    subparser.add_argument("--json", action="store_true",
                           help="print the resolved state as JSON (no key, no code)")
    subparser.add_argument("--no-qr", action="store_true",
                           help="skip the QR entirely (state only)")
    subparser.add_argument("--no-tailscale", action="store_true",
                           help="do not probe tailscale for TLS/hostname facts")
    subparser.add_argument("--ascii", action="store_true",
                           help="draw the QR with ASCII blocks instead of half-blocks")
    subparser.set_defaults(func=mobile_command)


def _read_stdin_password() -> Optional[str]:
    if sys.stdin is None or sys.stdin.isatty():
        return None
    data = sys.stdin.read()
    # Keep exact characters; drop only the trailing newline.
    return data.rstrip("\n") or None


def mobile_command(args: "argparse.Namespace") -> int:
    """Entry point set as ``func`` by :func:`register_cli`."""
    try:
        state = pairing.collect_state(
            label_override=getattr(args, "label", None),
            host_override=getattr(args, "host", None),
            port_override=getattr(args, "port", None),
            dashboard_override=getattr(args, "dashboard", None),
            dashboard_user_override=getattr(args, "dash_user", None),
            use_tailscale=not getattr(args, "no_tailscale", False),
        )
    except Exception as exc:  # never traceback into the user's terminal
        print(f"hermes mobile: could not inspect this install: {exc}", file=sys.stderr)
        return 1

    key, key_source = pairing.read_api_key()
    state.fields.append(pairing.Field("token", "present" if key else None, key_source))

    if getattr(args, "json", False):
        payload = {
            "host": state.chat_host,
            "port": state.chat_port,
            "label": state.label,
            "https": state.use_https,
            "dashboard": state.dashboard_url,
            "dash_user": state.dashboard_user,
            "token": "present" if key else "missing",
            "warnings": state.warnings,
            "unchecked": state.unchecked,
            "fields": [
                {"name": f.name, "value": f.value, "source": f.source, "note": f.note}
                for f in state.fields if f.name != "token"
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0 if key and state.chat_host else 1

    print(pairing.render_report(state))

    if not key:
        print(f"  Cannot build a pairing code: {key_source}.")
        print("  Set API_SERVER_KEY in .env (or keep exactly one strong entry) and rerun.", "\n")
        return 1
    if not state.chat_host or not state.chat_port:
        print("  Cannot build a pairing code: no reachable address/port was resolved.", "\n")
        return 1

    dash_pass = _read_stdin_password() if getattr(args, "dash_pass_stdin", False) else None
    if getattr(args, "dash_pass_stdin", False) and not dash_pass:
        print("  --dash-pass-stdin was given but stdin was empty or a terminal; "
              "Dashboard sign-in left out.", "\n")

    payload = pairing.PairPayload(
        host=state.chat_host,
        port=int(state.chat_port),
        token=key,
        label=state.label,
        use_https=state.use_https,
        dashboard_url=state.dashboard_url,
        dashboard_user=state.dashboard_user,
        dashboard_password=dash_pass,
    )
    uri = pairing.build_pair_uri(payload)

    if getattr(args, "no_qr", False) and not getattr(args, "link", False):
        return 0

    if getattr(args, "link", False):
        # Explicit opt-in: the link is a credential, printed once, on request.
        print("  Pairing link (credential — do not share):")
        print(f"  {uri}")
        print("\n  In the app: Options → Paste a link.")
        print(f"  {_NEEDS}\n")
        return 0

    print("  Scan with Hermes Console: Options → Scan a new code")
    try:
        art = qrterm.render(uri, ascii_fallback=getattr(args, "ascii", False))
    except qrterm.QRError as exc:
        print(f"\n  {exc}\n")
        return 2
    print()
    print(art)
    print()
    print(f"  The code carries the Gateway key and this machine's address — treat the screen")
    print(f"  as a credential while it is visible. {_NEEDS}\n")
    return 0
