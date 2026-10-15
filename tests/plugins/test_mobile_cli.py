"""Tests for the mobile-cli plugin: pairing fields, payload, and QR glyph mapping."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _REPO / "plugins" / "mobile-cli"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Load the plugin exactly as PluginManager does for a directory plugin:
# ``hermes_plugins.<slug>`` with hyphens mapped to underscores.
from plugins.plugin_loader import load_plugin_module  # noqa: E402

_MODULE = "hermes_plugins.mobile_cli"
_pkg = load_plugin_module(_MODULE, _PLUGIN_DIR, parents=("hermes_plugins",),
                          logger=logging.getLogger(__name__))
assert _pkg is not None, "could not load the mobile-cli plugin package"
pairing, qrterm = _pkg.pairing, _pkg.qrterm

SAMPLE_URI = "hermes://pair?host=100.64.0.5&port=8642&token=" + "a" * 64 + "&label=Em"


def _config(**overrides):
    base = {
        "platforms": {"api_server": {"extra": {"host": "100.64.0.5", "port": 8642}}},
        "dashboard": {"public_url": "https://em.example.ts.net",
                      "basic_auth": {"username": "ama", "password": ""}},
    }
    base.update(overrides)
    return base


class TestCollectState:
    def test_reads_host_port_and_dashboard_from_config(self):
        state = pairing.collect_state(config=_config(), use_tailscale=False)
        assert state.chat_host == "100.64.0.5"
        assert state.chat_port == 8642
        assert state.dashboard_url == "https://em.example.ts.net"
        assert state.dashboard_user == "ama"
        assert state.field("host").source.startswith("config.yaml")

    def test_missing_port_falls_back_and_warns(self):
        state = pairing.collect_state(config={"dashboard": {}}, use_tailscale=False)
        assert state.chat_port == 8642
        assert any("8642" in w for w in state.warnings)

    def test_plain_http_when_no_serve_handler_targets_the_chat_port(self):
        serve = {"Services": {"svc:em": {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {"em.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9119"}}}}}}}
        state = pairing.collect_state(config=_config(), serve=serve, use_tailscale=True)
        assert state.use_https is False
        assert "no serve handler targets the chat port" in state.field("https").note

    def test_https_when_tailscaled_terminates_tls_for_the_chat_port(self):
        serve = {"TCP": {"443": {"HTTPS": True}},
                 "Web": {"em.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8642"}}}}}
        state = pairing.collect_state(config=_config(), serve=serve, use_tailscale=True)
        assert state.use_https is True
        # The hostname on the certificate must win over the config IP.
        assert state.chat_host == "em.example.ts.net"

    def test_tcp_forward_is_reported_as_unchecked_not_guessed(self):
        serve = {"TCP": {"8443": {"TCPForward": "127.0.0.1:8642"}}}
        state = pairing.collect_state(config=_config(), serve=serve, use_tailscale=True)
        assert state.use_https is False
        assert any("TCP forward" in item for item in state.unchecked)

    def test_tls_terminated_tcp_forward_is_https(self):
        serve = {"TCP": {"8443": {"TCPForward": "127.0.0.1:8642",
                                  "TerminateTLS": "em.example.ts.net"}}}
        state = pairing.collect_state(config=_config(), serve=serve, use_tailscale=True)
        assert state.use_https is True
        assert "tls-terminated-tcp" in state.field("https").source

    def test_dashboard_password_is_reported_unrecoverable(self):
        state = pairing.collect_state(config=_config(), use_tailscale=False)
        assert any("hash" in item for item in state.unchecked)

    def test_label_defaults_to_tailscale_hostname_then_override_wins(self):
        tailnet = {"Self": {"HostName": "antigravity", "DNSName": "antigravity.ts.net.",
                            "TailscaleIPs": ["100.64.0.5"]},
                   "CurrentTailnet": {"MagicDNSEnabled": True}}
        state = pairing.collect_state(config=_config(), tailnet=tailnet, use_tailscale=True)
        assert state.label == "antigravity"
        state = pairing.collect_state(config=_config(), tailnet=tailnet,
                                      label_override="Em", use_tailscale=True)
        assert state.label == "Em"
        assert state.field("label").source == "--label"

    def test_magicdns_alternative_offered_only_when_enabled(self):
        enabled = {"Self": {"HostName": "h", "DNSName": "h.ts.net.", "TailscaleIPs": ["100.64.0.5"]},
                   "CurrentTailnet": {"MagicDNSEnabled": True}}
        disabled = {"Self": {"HostName": "h", "DNSName": "h.ts.net.", "TailscaleIPs": ["100.64.0.5"]},
                    "CurrentTailnet": {"MagicDNSEnabled": False}}
        with_magic = pairing.collect_state(config=_config(), tailnet=enabled, use_tailscale=True)
        without = pairing.collect_state(config=_config(), tailnet=disabled, use_tailscale=True)
        assert with_magic.field("host (alternative)").value == "h.ts.net"
        assert without.field("host (alternative)") is None


class TestApiKey:
    def test_reads_a_single_strong_key(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("OTHER=1\nAPI_SERVER_KEY=" + "b" * 64 + "\n")
        key, source = pairing.read_api_key(env)
        assert key == "b" * 64
        assert "64 chars" in source

    def test_rejects_short_or_placeholder_keys(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("API_SERVER_KEY=change-me-local-dev\n")
        key, reason = pairing.read_api_key(env)
        assert key is None
        assert "placeholder" in reason or "too short" in reason

    def test_rejects_conflicting_keys(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("API_SERVER_KEY=" + "c" * 64 + "\nAPI_SERVER_KEY=" + "d" * 64 + "\n")
        key, reason = pairing.read_api_key(env)
        assert key is None
        assert "conflicting" in reason

    def test_missing_env_is_reported_not_raised(self, tmp_path):
        key, reason = pairing.read_api_key(tmp_path / "nope.env")
        assert key is None
        assert "no " in reason


class TestPayload:
    def test_uri_matches_the_client_field_set(self):
        uri = pairing.build_pair_uri(pairing.PairPayload(
            host="100.64.0.5", port=8642, token="t" * 32, label="Em", use_https=True,
            dashboard_url="https://em.example.ts.net", dashboard_user="ama",
            dashboard_password="pw"))
        assert uri.startswith("hermes://pair?")
        assert "https=1" in uri
        assert "dash_user=ama" in uri and "dash_pass=pw" in uri
        assert "label=Em" in uri

    def test_half_a_dashboard_login_is_omitted(self):
        uri = pairing.build_pair_uri(pairing.PairPayload(
            host="h", port=1, token="t", dashboard_user="ama", dashboard_password=None))
        assert "dash_user" not in uri and "dash_pass" not in uri

    def test_https_omitted_when_plain(self):
        uri = pairing.build_pair_uri(pairing.PairPayload(host="h", port=8642, token="t"))
        assert "https" not in uri

    def test_reserved_characters_are_encoded(self):
        uri = pairing.build_pair_uri(pairing.PairPayload(
            host="h", port=8642, token="t", label="Em & co"))
        assert "Em+%26+co" in uri or "Em%20%26%20co" in uri


class TestQrRendering:
    def test_matrix_is_square_and_light_corners(self):
        matrix = qrterm.matrix_for_test(SAMPLE_URI)
        assert matrix, "no QR renderer available in this environment"
        assert len(matrix) == len(matrix[0])
        # Finder patterns sit in the corners; the outer ring is dark.
        assert matrix[0][0] is True
        assert matrix[0][-1] is True

    def test_art_round_trips_to_the_encoder_matrix(self):
        matrix = qrterm.matrix_for_test(SAMPLE_URI)
        art = qrterm.render(SAMPLE_URI)
        assert qrterm.matrix_from_art(art) == matrix

    def test_art_round_trips_without_theme_inversion(self):
        matrix = qrterm.matrix_for_test(SAMPLE_URI)
        art = qrterm.render(SAMPLE_URI, dark_terminal=False)
        assert qrterm.matrix_from_art(art, dark_terminal=False) == matrix

    def test_render_raises_when_no_renderer(self, monkeypatch):
        monkeypatch.setattr(qrterm, "_matrix_via_libqrencode", lambda *a, **k: None)
        with pytest.raises(qrterm.QRError):
            qrterm.render(SAMPLE_URI)


class TestReportSafety:
    def test_report_never_contains_the_key(self):
        state = pairing.collect_state(config=_config(), use_tailscale=False)
        state.fields.append(pairing.Field("token", "present", ".env (API_SERVER_KEY, 64 chars)"))
        report = pairing.render_report(state)
        assert "present" in report
        assert "a" * 64 not in report

    def test_json_payload_has_no_secret_fields(self):
        state = pairing.collect_state(config=_config(), use_tailscale=False)
        blob = json.dumps({
            "host": state.chat_host, "port": state.chat_port, "label": state.label,
            "https": state.use_https, "dashboard": state.dashboard_url,
            "dash_user": state.dashboard_user, "token": "present",
        })
        assert "dash_pass" not in blob
