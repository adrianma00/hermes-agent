"""A success must clear a code-less ``failure_reason`` on the pool entry.

``failure_reason`` is the classifier's verdict about the last failure and lives in
``extra`` (not a dataclass field), so it survives serialization into ``auth.json``.
The pool writer only dropped it on the exhaustion path, so an entry that was later
marked healthy kept a stale ``billing`` verdict next to ``last_status=ok`` with no
``last_error_code`` / ``last_error_message`` / ``last_error_reset_at`` to corroborate
it. Read back by audit/introspection tooling (and by the provider-wall notice before
it started requiring corroboration), that painted a working route as walled.

The reachable production shape (em / Yummi / Heimdall, 2026-10-13): the model name
``deepseek-v4-flash`` exists on two providers, so when modelark-coding refused for
billing the refusal was recorded against the *deepseek fallback's* pool entry; the
fallback then served the request successfully and the verdict outlived the failure.

Driven entirely through the pool API — no hand-written ``auth.json`` row.
"""

from __future__ import annotations

import time

import pytest

import agent.credential_pool as cp

PROVIDER = "deepseek"


class _Clock:
    """Stand-in for the ``time`` module so a cooldown can elapse without waiting.

    Only ``time()`` is shifted; everything else (``monotonic``, used by the auth-store
    lock) delegates to the real module.
    """

    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    def __init__(self, now: float):
        self._now = now

    def time(self) -> float:
        return self._now

    def __getattr__(self, name: str):
        return getattr(time, name)


def _advance(monkeypatch, seconds: float) -> None:
    """Move both pool-side clocks past the cooldown.

    ``hermes_cli.auth`` re-derives the on-disk cooldown during the persist merge, so
    patching only ``agent.credential_pool`` would leave the merge reading a stale bench.
    """
    from hermes_cli import auth as auth_mod

    clock = _Clock(time.time() + seconds)
    monkeypatch.setattr(cp, "time", clock)
    monkeypatch.setattr(auth_mod, "time", clock)


def _pool(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv(f"{PROVIDER.upper()}_API_KEY", raising=False)
    from agent.credential_pool import CredentialPool, PooledCredential

    pool = CredentialPool(PROVIDER, [])
    pool.add_entry(
        PooledCredential(
            provider=PROVIDER,
            id="fallback-1",
            label="fallback",
            auth_type="api_key",
            priority=0,
            source="manual",
            access_token="sk-fallback",
            base_url="https://api.deepseek.com",
        )
    )
    return pool


def _persisted_row(pool) -> dict:
    from agent.credential_pool import read_credential_pool

    return next(row for row in read_credential_pool(PROVIDER) if row.get("id") == "fallback-1")


def test_fallback_entry_ends_clean_after_the_route_succeeds(tmp_path, monkeypatch):
    """Primary billing refusal recorded on the fallback entry, then a success ⇒ clean."""
    pool = _pool(tmp_path, monkeypatch)

    # The refusal came from the primary (modelark-coding) but the classified verdict
    # lands on the deepseek fallback entry — same model name on both providers.
    pool.mark_exhausted_and_rotate(
        status_code=403,
        failure_reason="billing",
        credential_id="fallback-1",
        error_context={"message": "insufficient credits"},
    )
    benched = pool.entries()[0]
    assert benched.last_status == cp.STATUS_EXHAUSTED
    assert benched.failure_reason == "billing"

    # The bench expires (the pool TTL elapsed, as it did on the three installs) and the
    # entry is leased again — the fallback request succeeds.
    _advance(monkeypatch, cp.EXHAUSTED_TTL_DEFAULT_SECONDS + 60)
    recovered = pool.select()
    assert recovered is not None and recovered.id == "fallback-1"
    assert recovered.last_status == cp.STATUS_OK

    # The success must leave no verdict behind: nothing on the row corroborates it.
    assert recovered.failure_reason is None
    assert "failure_reason" not in recovered.extra
    assert recovered.last_error_code is None
    assert recovered.last_error_message is None
    assert recovered.last_error_reset_at is None

    row = _persisted_row(pool)
    assert row.get("last_status") == cp.STATUS_OK
    assert row.get("failure_reason") is None
    assert row.get("last_error_code") is None


def test_success_clears_a_verdict_with_no_corroboration(tmp_path, monkeypatch):
    """The acceptance case: a verdict with no code/message/reset is cleared by a success.

    This is the row shape read off the three installs — ``failure_reason`` set,
    ``last_error_code`` / ``last_error_message`` / ``last_error_reset_at`` all empty —
    so a consumer cannot corroborate the verdict from anything else on the entry.
    """
    pool = _pool(tmp_path, monkeypatch)
    pool.mark_exhausted_and_rotate(
        status_code=None,
        failure_reason="billing",
        credential_id="fallback-1",
    )
    benched = pool.entries()[0]
    assert benched.failure_reason == "billing"
    assert benched.last_error_code is None
    assert benched.last_error_message is None
    assert benched.last_error_reset_at is None

    _advance(monkeypatch, cp.EXHAUSTED_TTL_DEFAULT_SECONDS + 60)
    recovered = pool.select()
    assert recovered is not None and recovered.last_status == cp.STATUS_OK
    assert recovered.failure_reason is None
    assert "failure_reason" not in recovered.extra
    assert _persisted_row(pool).get("failure_reason") is None


def test_status_clear_after_reauth_drops_the_verdict(tmp_path, monkeypatch):
    """A ``_CLEAR_STATUS`` transition (re-auth / token resync) drops the verdict too."""
    pool = _pool(tmp_path, monkeypatch)
    pool.mark_exhausted_and_rotate(
        status_code=400,
        failure_reason="billing",
        credential_id="fallback-1",
        error_context={"message": "extra usage exhausted"},
    )
    entry = pool.entries()[0]

    cleared = pool._adopt(entry, **cp._CLEAR_STATUS)
    assert cleared.last_status is None
    assert cleared.failure_reason is None
    assert "failure_reason" not in cleared.extra


def test_a_token_rotation_clears_the_verdict(tmp_path, monkeypatch):
    """A rotated credential makes the old verdict stale (``_upsert_entry`` path)."""
    pool = _pool(tmp_path, monkeypatch)
    pool.mark_exhausted_and_rotate(
        status_code=403,
        failure_reason="billing",
        credential_id="fallback-1",
        error_context={"message": "insufficient credits"},
    )
    assert pool.entries()[0].failure_reason == "billing"

    # A rotated token arrives through the seeder's merge path.
    cp._upsert_entry(
        pool._entries,
        PROVIDER,
        "manual",
        {"access_token": "sk-rotated", "last_status": None},
    )
    merged = pool.entries()[0]
    assert merged.access_token == "sk-rotated"
    assert merged.last_status is None
    assert merged.failure_reason is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
