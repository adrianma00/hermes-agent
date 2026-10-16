"""Credential shredding in the scratch prune: a PEM/Vault-token-shaped file goes on sight.

The scan runs BEFORE the age-based pass, so a secret written a second ago is removed on the
next tick instead of waiting out the 24h idle window. These tests are the only guard on that
behaviour: an upstream rewrite of ``prune_idle_entries`` that keeps the age pass and drops the
scan must redden something.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_constants_scratch as scratch

KEY_MATERIAL = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\n"
VAULT_TOKEN = "hvs.CAESIJ0abcdefghijklmnopqrstuvwxyz0123456789\n"


def _write(root: Path, name: str, body: str = KEY_MATERIAL) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_prune_idle_entries_shreds_credential_shaped_files_before_the_age_pass(tmp_path):
    """The wiring that matters: the public entry point shreds a FRESH secret.

    A file written this second is nowhere near the idle window, so it can only disappear
    because the credential scan ran ahead of the age-based pass. If a future rewrite keeps
    the age pass and drops the scan, this is the test that fails.
    """
    root = tmp_path / "scratch"
    root.mkdir()
    key = _write(root, "id_rsa", KEY_MATERIAL)
    token = _write(root, "vault-token", VAULT_TOKEN)
    plain = _write(root, "notes.txt", "nothing secret here\n")

    scratch.prune_idle_entries(root, 24, frozenset())

    assert not key.exists(), "a fresh PEM key must not survive until the 24h idle window"
    assert not token.exists(), "a fresh Vault token must not survive until the 24h idle window"
    assert plain.exists(), "the scan must not touch files that are not credential-shaped"


def test_scan_reports_its_own_removal_count_and_is_idempotent(tmp_path):
    """The helper counts what it removed, and a second pass finds nothing left to remove."""
    root = tmp_path / "scratch"
    root.mkdir()
    _write(root, "a.key")
    _write(root, "nested/b.key")
    plain = _write(root, "keep.txt", "ordinary scratch content\n")

    assert scratch._scan_and_shred_credentials(root) == 2
    assert not (root / "a.key").exists()
    assert not (root / "nested" / "b.key").exists()
    assert plain.exists()
    assert scratch._scan_and_shred_credentials(root) == 0


@pytest.mark.parametrize("header", sorted(scratch._CREDENTIAL_HEADERS))
def test_every_known_credential_header_matches(tmp_path, header):
    """Each pattern in the table is load-bearing: dropping one silently un-guards a format."""
    root = tmp_path / "scratch"
    root.mkdir()
    secret = _write(root, "secret", f"{header}\nabc123\n")

    assert scratch._scan_and_shred_credentials(root) == 1
    assert not secret.exists()


@pytest.mark.parametrize("near_miss", [
    "-----BEGIN RSA PRIVATE KEY----",      # one dash short of the header
    "-----BEGIN PUBLIC KEY-----",          # a PUBLIC key is not a secret
    "hvs",                                 # the Vault prefix without its dot
    "x hvs.CAESIJ0abcdef",                 # the token not at the start of the line
    "-----BEGIN CERTIFICATE-----",         # a certificate is public material
])
def test_near_miss_prefixes_are_left_alone(tmp_path, near_miss):
    """Prefix matching, not substring or fuzzy matching: ordinary text never matches."""
    root = tmp_path / "scratch"
    root.mkdir()
    innocent = _write(root, "innocent.txt", f"{near_miss}\nbody\n")

    assert scratch._scan_and_shred_credentials(root) == 0
    assert innocent.exists()


def test_unreadable_and_binary_files_do_not_stop_the_scan(tmp_path):
    """One file that cannot be read must not abort the walk for the rest of the tree."""
    root = tmp_path / "scratch"
    root.mkdir()
    (root / "binary.bin").write_bytes(b"\x00\xff\xfe" * 100)
    dangling = root / "dangling"
    dangling.symlink_to(root / "does-not-exist")
    secret = _write(root, "deeper/id_ed25519", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")

    assert scratch._scan_and_shred_credentials(root) == 1
    assert not secret.exists()
    assert (root / "binary.bin").exists(), "binary content is not credential-shaped"


def test_first_line_truncates_at_the_first_newline_and_survives_bad_input(tmp_path):
    """The probe reads one line and never raises; a missing path is simply empty."""
    root = tmp_path / "scratch"
    root.mkdir()
    multi = _write(root, "multi.txt", "first\nsecond\nthird\n")

    assert scratch._first_line(multi) == "first"
    assert scratch._first_line(root / "missing") == ""
    assert scratch._first_line(root) == ""
