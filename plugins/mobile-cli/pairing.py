"""Pair a mobile Hermes client (e.g. Hermes Console) with this install.

Reads the facts back from the places that actually hold them — ``config.yaml``
(the profile config), ``.env`` (the gateway key) and ``tailscale`` (whether the
connection is TLS-terminated, and under which hostname) — and then renders the
``hermes://pair`` payload as a QR code.

The key never leaves this process except as pixels: it is not printed, not
logged, not passed in argv, and not included in ``--json`` output.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "Field",
    "MobileState",
    "PairPayload",
    "build_pair_uri",
    "collect_state",
    "load_config_yaml",
    "read_api_key",
    "render_report",
]

# A credential shorter than this is treated as a placeholder, never a key.
_MIN_KEY_LEN = 16
_PLACEHOLDERS = {"change-me", "change-me-local-dev", "changeme", "hermes", "none", "null"}

_TAILSCALE_TIMEOUT = 10


@dataclass
class Field:
    """One resolved pairing field plus where it came from (or why it is absent)."""

    name: str
    value: Optional[str]
    source: str
    note: str = ""

    @property
    def resolved(self) -> bool:
        return self.value not in (None, "")


@dataclass
class MobileState:
    """Everything gathered about this install, with provenance for each field."""

    fields: List[Field] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    unchecked: List[str] = field(default_factory=list)
    dashboard_host: Optional[str] = None
    chat_host: Optional[str] = None
    chat_port: Optional[int] = None
    use_https: bool = False
    dashboard_url: Optional[str] = None
    label: Optional[str] = None
    dashboard_user: Optional[str] = None

    def field(self, name: str) -> Optional[Field]:
        for item in self.fields:
            if item.name == name:
                return item
        return None


@dataclass
class PairPayload:
    """The values that go into ``hermes://pair?...``."""

    host: str
    port: int
    token: str
    label: Optional[str] = None
    use_https: bool = False
    dashboard_url: Optional[str] = None
    dashboard_user: Optional[str] = None
    dashboard_password: Optional[str] = None


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def hermes_home() -> Path:
    """Resolve the profile home the way the rest of the CLI does."""
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else Path.home() / ".hermes"


def load_config_yaml(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read ``config.yaml`` as a plain dict; never raise on a broken/absent file."""
    target = path or (hermes_home() / "config.yaml")
    try:
        import yaml  # type: ignore
    except Exception:  # pragma: no cover - environment without PyYAML
        try:
            from hermes_cli import config as _cfg  # type: ignore

            loader = getattr(_cfg, "_load_yaml", None)
            if loader is None:
                return {}
            return loader(target.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _dig(data: Any, *keys: str) -> Any:
    cur = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def read_api_key(env_path: Optional[Path] = None) -> Tuple[Optional[str], str]:
    """Return ``(key, source_note)`` for the gateway key, or ``(None, reason)``.

    Uses ``.env`` only. The value is read, never echoed.
    """
    path = env_path or (hermes_home() / ".env")
    if not path.exists():
        return None, f"no {path}"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    found: List[str] = []
    for line in text.splitlines():
        if line.startswith("API_SERVER_KEY="):
            value = line.split("=", 1)[1].strip().strip("'\"")
            found.append(value)
    strong = [v for v in found if len(v) >= _MIN_KEY_LEN and v.lower() not in _PLACEHOLDERS]
    if not strong:
        if found:
            return None, "API_SERVER_KEY present but too short or a placeholder"
        return None, "no API_SERVER_KEY in .env"
    if len(set(strong)) > 1:
        return None, "conflicting API_SERVER_KEY entries in .env"
    return strong[0], f".env (API_SERVER_KEY, {len(strong[0])} chars)"


# --------------------------------------------------------------------------
# addresses
# --------------------------------------------------------------------------

def _tailscale_json() -> Optional[Dict[str, Any]]:
    if not shutil.which("tailscale"):
        return None
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True,
            timeout=_TAILSCALE_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _serve_json() -> Optional[Dict[str, Any]]:
    if not shutil.which("tailscale"):
        return None
    try:
        out = subprocess.run(
            ["tailscale", "serve", "status", "--json"], capture_output=True, text=True,
            timeout=_TAILSCALE_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _magicdns_name(tailnet: Optional[Dict[str, Any]]) -> Optional[str]:
    if not tailnet:
        return None
    enabled = _dig(tailnet, "CurrentTailnet", "MagicDNSEnabled")
    name = _dig(tailnet, "Self", "DNSName")
    if not isinstance(name, str) or not name:
        return None
    if enabled is False:
        return None
    return name.rstrip(".")


def _tailnet_ip(tailnet: Optional[Dict[str, Any]]) -> Optional[str]:
    ips = _dig(tailnet, "Self", "TailscaleIPs") or []
    for candidate in ips if isinstance(ips, list) else []:
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if addr.version == 4:
            return str(addr)
    return None


def _local_addresses() -> List[str]:
    """Private IPv4 addresses, Tailscale first — mirrors what a phone can reach."""
    found: List[str] = []
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True,
                             text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for match in re.finditer(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", out):
        addr = match.group(1)
        if addr.startswith(("127.", "169.254.")):
            continue
        iface = out[: match.start()].rstrip().split("\n")[-1].split()[1:2]
        if iface and re.match(r"^(lo|docker|br-|veth|virbr|podman|cni|lxc)", iface[0]):
            continue
        found.append(addr)
    cgnat = [a for a in found if a.startswith("100.")]
    lan = [a for a in found if re.match(r"^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)", a)]
    return cgnat + lan


def _serve_summary(serve: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten every serve handler into ``{host, port, mode, target_port}``.

    Covers both shapes a listener can take: an L7 web handler (``Web``, which is
    either HTTPS- or HTTP-terminated by tailscaled) and an L4 forward (``TCP``
    with ``TCPForward``), which has no Web block at all. The L4 case matters
    because tailscaled only adds TLS when ``TerminateTLS`` is set — guessing
    "https" there would hand the app a URL it cannot reach.
    """
    rows: List[Dict[str, Any]] = []
    if not serve:
        return rows
    blocks: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(serve.get("Web"), dict) or isinstance(serve.get("TCP"), dict):
        blocks.append(("node", serve))
    services = serve.get("Services")
    if isinstance(services, dict):
        for name, config in services.items():
            if isinstance(config, dict):
                blocks.append((str(name), config))

    for label, config in blocks:
        tcp = config.get("TCP") if isinstance(config.get("TCP"), dict) else {}
        web = config.get("Web") if isinstance(config.get("Web"), dict) else {}
        seen_ports: set = set()

        for hostport, server in web.items():
            if not isinstance(server, dict):
                continue
            handlers = server.get("Handlers") if isinstance(server.get("Handlers"), dict) else {}
            entry = handlers.get("/") if isinstance(handlers.get("/"), dict) else {}
            proxy = str(entry.get("Proxy") or "")
            target_port = _port_from_target(proxy)
            host, _, port_txt = str(hostport).rpartition(":")
            try:
                external_port = int(port_txt)
            except ValueError:
                external_port = 443
            mode = "unknown"
            spec = tcp.get(str(external_port)) if isinstance(tcp, dict) else None
            if isinstance(spec, dict):
                if spec.get("HTTPS"):
                    mode = "https"
                elif spec.get("HTTP"):
                    mode = "http"
                elif spec.get("TCPForward"):
                    mode = "tcp"
            seen_ports.add(external_port)
            rows.append({
                "service": label, "host": host, "external_port": external_port,
                "mode": mode, "target_port": target_port, "proxy": proxy,
                "terminate_tls": bool(isinstance(spec, dict) and spec.get("TerminateTLS")),
            })

        for port_txt, spec in (tcp.items() if isinstance(tcp, dict) else []):
            try:
                external_port = int(port_txt)
            except (TypeError, ValueError):
                continue
            if external_port in seen_ports or not isinstance(spec, dict):
                continue
            forward = spec.get("TCPForward")
            if not forward:
                continue
            rows.append({
                "service": label, "host": None, "external_port": external_port,
                "mode": "tcp", "target_port": _port_from_target(str(forward)),
                "proxy": str(forward), "terminate_tls": bool(spec.get("TerminateTLS")),
            })
    return rows


def _port_from_target(target: str) -> Optional[int]:
    """Trailing port of a ``host:port`` / ``http://host:port`` serve target."""
    match = re.search(r":(\d+)\s*$", target.strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _pem(host: str, port: int, https: bool) -> str:
    return f"{'https' if https else 'http'}://{host}" + ("" if https and port == 443 else f":{port}")


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------

def collect_state(
    *, config: Optional[Dict[str, Any]] = None, env_path: Optional[Path] = None,
    tailnet: Optional[Dict[str, Any]] = None, serve: Optional[Dict[str, Any]] = None,
    label_override: Optional[str] = None, host_override: Optional[str] = None,
    port_override: Optional[int] = None, dashboard_override: Optional[str] = None,
    dashboard_user_override: Optional[str] = None,
    use_tailscale: bool = True,
) -> MobileState:
    """Gather the pairing fields, recording the source (or the reason) for each."""
    state = MobileState()
    cfg = config if config is not None else load_config_yaml()

    if use_tailscale:
        tailnet = tailnet if tailnet is not None else _tailscale_json()
        serve = serve if serve is not None else _serve_json()

    # --- port ------------------------------------------------------------
    cfg_port = _dig(cfg, "platforms", "api_server", "extra", "port")
    try:
        port = int(port_override if port_override is not None else cfg_port)
    except (TypeError, ValueError):
        port = 8642
        state.warnings.append("no platforms.api_server.extra.port in config; assuming 8642")
    state.chat_port = port

    # --- TLS for the chat port (tailscale is the only real source) --------
    rows = _serve_summary(serve)
    chat_row = next((r for r in rows if r["target_port"] == port), None)
    if use_tailscale:
        if chat_row is None:
            state.use_https = False
            state.fields.append(Field(
                "https", None, "tailscale serve",
                "no serve handler targets the chat port — plain HTTP on the private network"))
        elif chat_row["mode"] == "https":
            state.use_https = True
            state.fields.append(Field("https", "1", "tailscale serve (HTTPS termination)"))
        elif chat_row["mode"] == "http":
            state.use_https = False
            state.fields.append(Field("https", None, "tailscale serve",
                                      "served over plain HTTP (--http); not TLS"))
        elif chat_row.get("terminate_tls"):
            # L4 forward with TLS terminated at tailscaled: the client side is HTTPS.
            state.use_https = True
            state.fields.append(Field(
                "https", "1", "tailscale serve (tls-terminated-tcp)",
                f"TLS ends at tailscaled on port {chat_row['external_port']}"))
        else:
            # L4 forward without TerminateTLS: what the client sees depends on the
            # upstream, so refuse to guess rather than publish a wrong scheme.
            state.fields.append(Field(
                "https", None, "tailscale serve (TCP forward)",
                f"{chat_row['proxy']} — raw TCP; TLS termination unknown"))
            state.unchecked.append(
                f"chat port {port} is behind a TCP forward ({chat_row['proxy']}) without "
                "TerminateTLS; TLS termination could not be determined")
    else:
        state.use_https = False
        state.fields.append(Field("https", None, "not probed", "--no-tailscale"))

    # --- host ------------------------------------------------------------
    cfg_host = _dig(cfg, "platforms", "api_server", "extra", "host")
    candidate = host_override or cfg_host
    source = "config.yaml (platforms.api_server.extra.host)" if cfg_host else None
    if not candidate:
        candidate = _tailnet_ip(tailnet) or next(iter(_local_addresses()), None)
        source = "tailscale/local address (no host in config)"
    if not candidate:
        state.warnings.append("no address found for the phone to connect to")
    if chat_row and chat_row["mode"] == "https":
        candidate, source = chat_row["host"], "tailscale serve (hostname on the certificate)"
    state.chat_host = candidate
    state.fields.append(Field("host", candidate, source or "unknown"))

    magic = _magicdns_name(tailnet)
    if magic and candidate and candidate != magic:
        state.fields.append(Field("host (alternative)", magic, "MagicDNS",
                                  "same machine; use if the raw IP is awkward to type"))

    # --- dashboard -------------------------------------------------------
    cfg_dash = _dig(cfg, "dashboard", "public_url")
    dash = dashboard_override or cfg_dash
    dash_source = "config.yaml (dashboard.public_url)" if cfg_dash else None
    if not dash and rows:
        own = next((r for r in rows if r["mode"] == "https"), None)
        if own:
            dash = _pem(own["host"], own["external_port"], True)
            dash_source = "tailscale serve (handler in the serve config)"
    if dash and not rows:
        state.unchecked.append("dashboard URL could not be checked against this node's serve config")
    state.dashboard_url = dash
    state.fields.append(Field("dashboard", dash, dash_source or "not found",
                              "" if dash else "the app's Dashboard screens will stay unconfigured"))

    # --- dashboard user --------------------------------------------------
    cfg_user = _dig(cfg, "dashboard", "basic_auth", "username")
    user = dashboard_user_override or cfg_user
    state.dashboard_user = user
    state.fields.append(Field("dash_user", user, "config.yaml (dashboard.basic_auth.username)"))

    if cfg_user and not dashboard_override:
        state.unchecked.append(
            "dashboard password is not recoverable (config.yaml keeps only a hash); "
            "the app will ask for it once, or use --dash-pass-stdin")

    # --- label -----------------------------------------------------------
    resolved_label = label_override
    label_source = "--label"
    if not resolved_label:
        hostname = _dig(tailnet, "Self", "HostName")
        resolved_label = hostname or (Path.home().name if Path.home().name else None)
        label_source = "tailscale hostname" if hostname else "local username"
    if not resolved_label:
        state.warnings.append("could not derive a display name; pass --label")
    state.label = resolved_label
    state.fields.append(Field("label", resolved_label, label_source,
                              "shown as the instance name in the app"))

    return state


# --------------------------------------------------------------------------
# payload + rendering
# --------------------------------------------------------------------------

def build_pair_uri(payload: PairPayload) -> str:
    """Build the canonical ``hermes://pair`` URI (the client's own field set)."""
    from urllib.parse import urlencode

    query: List[Tuple[str, str]] = [("host", payload.host), ("port", str(payload.port)),
                                    ("token", payload.token)]
    if payload.label:
        query.append(("label", payload.label))
    if payload.use_https:
        query.append(("https", "1"))
    if payload.dashboard_url:
        query.append(("dashboard", payload.dashboard_url))
    if payload.dashboard_user and payload.dashboard_password:
        query.append(("dash_user", payload.dashboard_user))
        query.append(("dash_pass", payload.dashboard_password))
    return "hermes://pair?" + urlencode(query)


def render_report(state: MobileState, *, show_source: bool = True) -> str:
    """Human-readable status. Never contains the key."""
    lines: List[str] = ["", "  Mobile pairing state", "  " + "-" * 46]
    for item in state.fields:
        value = item.value if item.resolved else "—"
        origin = f"  [{item.source}]" if show_source and item.source else ""
        lines.append(f"  {item.name:<18} {value}{origin}")
        if item.note:
            lines.append(f"  {'':<18} {item.note}")
    if state.warnings:
        lines.append("")
        lines.append("  Warnings")
        for warning in state.warnings:
            lines.append(f"    ! {warning}")
    if state.unchecked:
        lines.append("")
        lines.append("  Not checked / not available")
        for item in state.unchecked:
            lines.append(f"    · {item}")
    lines.append("")
    lines.append("  The Gateway key is read from .env and used only to build the payload below;")
    lines.append("  it is never printed by this command.")
    lines.append("")
    return "\n".join(lines)
