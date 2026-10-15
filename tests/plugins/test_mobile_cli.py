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

    def test_label_prefers_kanban_namespace_over_shared_tailscale_hostname(self):
        """Co-located installs share a tailscale hostname, so the namespace must win."""
        tailnet = {"Self": {"HostName": "antigravity", "DNSName": "antigravity.ts.net.",
                            "TailscaleIPs": ["100.64.0.5"]},
                   "CurrentTailnet": {"MagicDNSEnabled": True}}
        state = pairing.collect_state(config=_config(kanban={"namespace": "yummi"}),
                                      tailnet=tailnet, use_tailscale=True)
        assert state.label == "yummi"
        assert state.field("label").source == "config.yaml (kanban.namespace)"

        # --label still outranks the namespace
        state = pairing.collect_state(config=_config(kanban={"namespace": "yummi"}),
                                      tailnet=tailnet, label_override="Em", use_tailscale=True)
        assert state.label == "Em"
        assert state.field("label").source == "--label"

        # absent or blank namespace falls through to the hostname
        state = pairing.collect_state(config=_config(), tailnet=tailnet, use_tailscale=True)
        assert state.label == "antigravity"
        state = pairing.collect_state(config=_config(kanban={"namespace": "   "}),
                                      tailnet=tailnet, use_tailscale=True)
        assert state.label == "antigravity"
        # non-string namespace must not crash or name the connection "None"
        state = pairing.collect_state(config=_config(kanban={"namespace": None}),
                                      tailnet=tailnet, use_tailscale=True)
        assert state.label == "antigravity"

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


class TestLibrarySearchPath:
    """libqrencode installed by a package manager must be found even when the dynamic loader
    would not search its directory — which is the normal state on macOS with Homebrew."""

    def test_homebrew_prefixes_are_searched(self):
        paths = qrterm._candidate_paths()
        # /opt/homebrew is Apple-Silicon Homebrew; dyld does not search it for a plain process.
        assert "/opt/homebrew/lib/libqrencode.4.dylib" in paths
        assert "/opt/homebrew/lib/libqrencode.dylib" in paths
        assert "/usr/local/lib/libqrencode.dylib" in paths  # Intel Homebrew
        assert "/opt/local/lib/libqrencode.dylib" in paths  # MacPorts

    def test_debian_multiarch_dirs_are_searched(self):
        paths = qrterm._candidate_paths()
        assert "/usr/lib/x86_64-linux-gnu/libqrencode.so.4" in paths
        assert "/usr/lib/aarch64-linux-gnu/libqrencode.so.4" in paths

    def test_env_advertised_dirs_are_searched(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LD_LIBRARY_PATH", str(tmp_path))
        assert str(tmp_path / "libqrencode.so.4") in qrterm._candidate_paths()
        monkeypatch.delenv("LD_LIBRARY_PATH")
        monkeypatch.setenv("DYLD_FALLBACK_LIBRARY_PATH", str(tmp_path))
        assert str(tmp_path / "libqrencode.dylib") in qrterm._candidate_paths()

    def test_macos_naming_resolves_from_a_non_searchable_dir(self, tmp_path, monkeypatch):
        """Witnesses the macOS path without a Mac: same code, `libqrencode.4.dylib` naming, in a
        directory the loader would never search, with name-loading and the platform convention
        both disabled. This is the shape the Mac was failing in."""
        source = next((Path(p) for p in qrterm._candidate_paths() if Path(p).exists()), None)
        if source is None:
            pytest.skip("no libqrencode on this box to point at")
            return
        target = tmp_path / "libqrencode.4.dylib"
        target.write_bytes(source.read_bytes())

        monkeypatch.setattr(qrterm, "_LIB_NAMES", ("libqrencode.4.dylib",))
        monkeypatch.setattr(qrterm, "_LIB_DIRS", (str(tmp_path),))
        monkeypatch.setattr(qrterm, "_find_library_by_convention", lambda: None)
        monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
        monkeypatch.delenv("DYLD_FALLBACK_LIBRARY_PATH", raising=False)

        assert qrterm._load_library() is not None
        # usable, not merely loaded: the fallback must produce a real matrix
        matrix = qrterm._matrix_via_libqrencode(SAMPLE_URI)
        assert matrix and matrix[0][0] is True

    def test_returns_none_instead_of_raising_when_nothing_matches(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(qrterm, "_LIB_NAMES", ("libqrencode-does-not-exist.so",))
        monkeypatch.setattr(qrterm, "_LIB_DIRS", (str(tmp_path),))
        monkeypatch.setattr(qrterm, "_find_library_by_convention", lambda: None)
        monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
        monkeypatch.delenv("DYLD_FALLBACK_LIBRARY_PATH", raising=False)
        assert qrterm._load_library() is None

    def test_this_environment_still_resolves_a_library(self):
        assert qrterm._load_library() is not None, "no libqrencode available in this environment"


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


class TestConfigReadReporting:
    """An unreadable config must be LOUD: it is not the same claim as "not set here"."""

    def test_missing_config_reports_a_reason(self, tmp_path):
        data, reason = pairing.load_config_yaml_with_reason(tmp_path / "absent.yaml")
        assert data == {}
        assert reason and "not found" in reason

    def test_no_loader_is_a_reason_not_an_empty_dict(self, tmp_path, monkeypatch):
        target = tmp_path / "config.yaml"
        target.write_text("platforms: {}\n", encoding="utf-8")
        monkeypatch.setattr(pairing, "_yaml_safe_load", lambda: None)
        data, reason = pairing.load_config_yaml_with_reason(target)
        assert data == {}
        assert reason and "no YAML loader" in reason

    def test_unreadable_config_surfaces_as_a_warning_with_guesses_flagged(
            self, tmp_path, monkeypatch):
        """The Heimdall case: config present and readable, but no loader resolved."""
        (tmp_path / "config.yaml").write_text(
            "platforms:\n  api_server:\n    extra:\n      port: 8642\n", encoding="utf-8")
        monkeypatch.setattr(pairing, "hermes_home", lambda: tmp_path)
        monkeypatch.setattr(pairing, "_yaml_safe_load", lambda: None)
        state = pairing.collect_state(use_tailscale=False)
        assert any("could not read config.yaml" in w for w in state.warnings)
        assert any("may be WRONG" in w for w in state.warnings)
        # and the config-derived fields really are missing, which is what makes it a guess
        assert state.dashboard_user is None

    def test_a_readable_config_reports_no_such_warning(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text(
            "platforms:\n  api_server:\n    extra:\n      host: 100.64.0.5\n      port: 8642\n"
            "dashboard:\n  basic_auth:\n    username: ama\n", encoding="utf-8")
        monkeypatch.setattr(pairing, "hermes_home", lambda: tmp_path)
        state = pairing.collect_state(use_tailscale=False)
        assert not any("could not read config.yaml" in w for w in state.warnings)
        assert state.dashboard_user == "ama"
        assert state.chat_port == 8642

    def test_loader_resolves_in_this_environment(self):
        assert pairing._yaml_safe_load() is not None
