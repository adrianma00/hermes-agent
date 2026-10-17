# Fork customizations -- what `hermes-agent-private` carries on top of upstream

Private `main` is a **divergent line**, not a descendant of `NousResearch/hermes-agent`. As of
2026-10-16 it sits **152 commits ahead / 6,863 behind** upstream, with a footprint of **179 files**
(43 fork-only files + 136 edits to shared files) since the fork point.

This file is the inventory of that footprint, so a sync does not begin with rediscovery.

**Why it exists.** Two failure modes it closes, both observed on 2026-10-16:

1. A fork-only security control (`_scan_and_shred_credentials` in `hermes_constants_scratch.py`) had
   **no test at all**, so an upstream rewrite of that function could have dropped it with nothing
   failing. A customization nobody has written down is invisible to every gate.
2. During the three obsolescence calls we could not answer "is this patch still ours?" without reading
   upstream code by hand. With the `upstream-watch` column below, the daily probe can answer it.

**Read it with**: `~/.hermes/scripts/upstream_sync_probe.py` (daily 05:45 SGT) and the sync proposal at
`/home/ama/hermes-dev/UPSTREAM-SYNC-PROPOSAL.md`.

**The invariant the probe enforces.** Every file that exists on our line and NOT upstream must appear in
the machine-readable block at the bottom of this file. "Fork-only" means: added on our line since the
fork point, and `git cat-file -e upstream/main:<path>` fails. That set is exact, cheap, and unambiguous,
so it is the part that is machine-checked; anything unlisted -- or anything listed that no longer exists
-- is a finding on the next probe run.

**Scope.** This tracks FEATURES, not every patch. 67 of our 136 shared-file edits belong to the clusters
below; the rest are one-off fixes (desktop pooled-remote liveness, matrix darwin split, peer-DM exit,
prompt-cache fixes, ...). A one-off earns a row here only when it becomes recurring.

---

## 1. Quota gate + local state store

Upstream has no fleet-wide provider-quota gate. We carry one: a shared gate card per provider that holds
BOTH kanban cards and cron jobs while a subscription/metered window is closed, with an operator
`hermes quota release-all` switch honoured by both work sources, a local SQLite state store, and a
`--json` surface for script consumers.

- **Fork-only**: `hermes_cli/quota.py`, `hermes_cli/quota_state.py`, `hermes_cli/subcommands/quota.py`,
  `cron/scheduler_quota.py`, `agent/quota_gate_trigger.py`,
  `gateway/platforms/api_server_quota_gate.py`
- **Edits to shared**: `agent/chat_completion_helpers.py`, `agent/turn_api_error.py`, `cron/jobs.py`,
  `cron/scheduler.py`, `cron/scheduler_tick.py`, `gateway/platforms/api_server.py`,
  `gateway/platforms/api_server_runs.py`, `hermes_cli/config_defaults.py`, `hermes_cli/kanban_db.py`,
  `hermes_cli/kanban_db_dispatch.py`, `hermes_cli/main.py`, `locales/en.yaml`,
  `tests/cron/test_retirement_dispatch.py`
- **Guarded by**: `tests/hermes_cli/test_quota_state.py`, `test_quota_state_board.py`,
  `test_kanban_quota_release_all.py`, `tests/cron/test_cron_quota_release_all.py`,
  `tests/gateway/test_api_server_quota_gate.py`
- **Upstream-watch**: `quota_gate`, `release_all`, `quota_state`, `quota_hold` -- note upstream already
  ships `cron/quota_hold.py` (a per-job 429 retry-after hold). That is a different mechanism, and both
  files coexisted at the fork point; convergence is a candidate, not a duplicate.

## 2. Cost window (off-peak deferral)

A released job or card is routed to the metered fallback, and running that fallback inside its peak-price
hours costs double for no benefit on non-latency-critical work -- so a released item waits for the
fallback's off-peak window. `run_now` forces it immediately.

- **Fork-only**: `hermes_cli/cost_window.py`, `hermes_cli/kanban_cost_window.py`,
  `hermes_cli/subcommands/cost_window.py`
- **Edits to shared**: `hermes_cli/config_defaults.py`, `hermes_cli/kanban.py`, `hermes_cli/kanban_db.py`,
  `hermes_cli/kanban_db_connect.py`, `hermes_cli/kanban_db_dispatch.py`, `hermes_cli/kanban_parser.py`,
  `hermes_cli/main.py`, `tools/kanban_tools.py`, `tools/kanban_tools_schemas.py`
- **Guarded by**: `tests/hermes_cli/test_cost_window_holidays.py`,
  `tests/cron/test_cron_cost_window_release.py`
- **Upstream-watch**: `cost window`, `off-peak`, `peak`

## 3. Kanban install namespaces

Upstream's kanban has a per-card `tenant` (a FILTER, "a soft namespace within a board"). We carry
`kanban.namespace`: an install-scoped identity that assigns boards to installs, gates cross-fleet
assignees, and pins the DB path per install. Upstream has no `namespace` concept -- verified 2026-10-07,
zero hits.

- **Fork-only**: 9 test files (`tests/hermes_cli/test_kanban_assignee_namespaces.py`,
  `test_kanban_board_pin_scope.py`, `test_kanban_cross_fleet_assignee_gate.py`, `test_kanban_gate_holds.py`,
  `test_kanban_namespace_cache.py`, `test_kanban_per_profile_cap_namespaced.py`,
  `test_kanban_phantom_refs.py`, `test_kanban_shared_dir_mode.py`,
  `test_kanban_write_guard_purge_survival.py`). The implementation itself lives in shared files.
- **Edits to shared**: `cli-config.yaml.example`, `gateway/kanban_watchers.py`,
  `hermes_cli/config_defaults.py`, `hermes_cli/kanban.py`, `hermes_cli/kanban_db.py`,
  `hermes_cli/kanban_db_connect.py`, `hermes_cli/kanban_db_dispatch.py`, `tests/conftest.py`,
  `website/docs/user-guide/features/kanban.md`
- **Edit site MOVED by the 2026-10-16 upstream merge**: the board-metadata cluster now lives in
  `hermes_cli/kanban_db_boards.py`, and `kanban_db.py` **re-exports** it. Our `namespace` parameter
  and `_caller_effective_db_path` were ported into the canonical module (`write_board_metadata`,
  `create_board`, `read_board_metadata`, plus the caller-effective `db_path` the board-pin tests
  depend on); the in-place copies the merge had kept in `kanban_db.py` were 197 lines of dead
  duplicate and are deleted. **Edit the canonical module, never `kanban_db.py` — a copy left there is
  shadowed by the re-export and silently dead code** (that exact trap produced 19 test errors on the
  merge: `create_board() got an unexpected keyword argument 'namespace'`).
- **Upstream-watch**: `namespace`, `tenant`, upstream PR 130854 (reserved/unspawnable assignee refusal)
- **Every site that turns an `assignee` into a profile home must strip the qualifier first**
  (`assignee_profile()` in `kanban_db_dispatch.py`) -- `resolve_profile_env()` rejects a colon
  outright, so a raw `"em:default"` raises `ValueError` there. Ported sites: `kanban_db_dispatch.py`
  (spawn), `gateway/session_kanban.py` (claim admission), `hermes_cli/kanban_pr_acceptance.py`
  (completion-contract gate). This class landed **twice** before it was caught -- the 2026-10-16
  board-metadata move, then the 2026-10-17 gateway claim-admission path, which refused every
  namespaced card as an opaque `invalid_kanban_claim`. `tests/hermes_cli/
  test_kanban_namespace_profile_lookup_guard.py` is the standing detector: it fails on any new
  `resolve_profile_env(<raw assignee>)` and on any blessed site that loses its `assignee_profile()`
  call, so a merge cannot silently drop the strip again. Extend its `_BLESSED` map when adding a site.
- **Admission re-checks the qualifier, it does not merely strip it** (`gateway/session_kanban.py`):
  a card qualified with another install's namespace is refused `namespace_mismatch`, because both
  installs have a `default` and only the qualifier says which one the card is for. The dispatcher's
  namespace gate normally refuses such a card first; admission re-checks so a path that never passed
  that gate (manual claim, stale lock, the other install's dispatcher writing into the shared DB)
  cannot run another install's card as this install's `default`.

## 4. Mobile pairing CLI

`hermes mobile` -- plugin that pairs a phone app to a self-hosted install, deriving the pairing label
from `kanban.namespace` and rendering a QR in the terminal. Self-contained: it edits no shared file.

- **Fork-only**: `plugins/mobile-cli/__init__.py`, `cli.py`, `pairing.py`, `qrterm.py`, `plugin.yaml`,
  `README.md`
- **Edits to shared**: none
- **Guarded by**: `tests/plugins/test_mobile_cli.py`
- **Upstream-watch**: `mobile`, `pairing`, `qr`, `plugins` endpoint

## 5. Provider-wall notice

When a provider route walls (billing/quota refusal), one operator-facing notice is written and surfaced
in the originating chat, fanned out to every home channel -- rather than each surface inventing its own.

- **Fork-only**: `agent/provider_wall_notice.py`
- **Edits to shared**: `agent/chat_completion_helpers.py`, `agent/turn_response_check.py`,
  `gateway/run.py`, `gateway/run_notifications.py`, `gateway/run_startup.py`,
  `gateway/run_turn_runner.py`, `hermes_cli/kanban_db.py`, `hermes_cli/kanban_db_dispatch.py`, plus
  all 17 `locales/*.yaml` catalogs (one `provider_wall:` key; `*.tui.yaml` is a separate catalog set)
- **Guarded by**: `tests/agent/test_provider_wall_notice.py`,
  `tests/gateway/test_provider_wall_notice_delivery.py`
- **Upstream-watch**: `provider wall`, `wall notice`, `billing`

## 6. Fallback / reasoning pinning

Fixes in the fallback and reasoning path: a pinned reasoning effort survives a route change
(`reasoning_config_explicit`), an overloaded retry budget is bounded, and a stale
`failure_reason` is dropped when the error state clears.

- **Fork-only**: no modules; three tests
- **Edits to shared**: `agent/agent_init.py`, `agent/agent_runtime_helpers.py`,
  `agent/chat_completion_helpers.py`, `agent/credential_pool.py`, `cron/scheduler.py`,
  `hermes_cli/cli_agent_setup_mixin.py`, `hermes_cli/oneshot.py`, `hermes_constants.py`, `run_agent.py`,
  `tui_gateway/server.py`
- **Guarded by**: `tests/agent/test_fallback_reasoning_pin.py`,
  `tests/agent/test_overloaded_retry_budget.py`,
  `tests/agent/test_credential_pool_stale_failure_reason.py`
- **Upstream-watch**: `reasoning_config_explicit`, `push_reasoning`, `failure_reason`, `fallback_models`

## 7. Smart-approval degraded notice

When the guardian (smart approval) cannot run, every prompt surface says WHY the human is being asked --
manual mode, or a fallback-route verdict -- instead of a silent, unexplained prompt.

- **Fork-only**: no modules; three tests
- **Edits to shared**: `gateway/platforms/base.py`, `gateway/run.py`, `gateway/run_turn_runner.py`,
  `tools/approval.py`, `tools/approval_prompt.py`, `tools/approval_smart.py`, `tools/terminal_tool.py`,
  `tui_gateway/contracts/common.py`, `apps/desktop/src/components/assistant-ui/tool/approval.test.tsx`,
  `apps/desktop/src/i18n/{de,en,es,fr}.ts`, `tests/gateway/test_exec_approval_template.py`
- **Guarded by**: `tests/tools/test_smart_approval_degraded_notice.py`,
  `tests/tools/test_smart_approval_degraded_surfaces.py`, `tests/tools/test_smart_approval_offloop.py`
- **Upstream-watch**: `smart_unavailable`, `guardian`, `degraded`

## 8. TUI interrupt attribution

A lifecycle interrupt is attributed to its system producer, so the TUI reports which subsystem
interrupted a turn rather than an anonymous break.

- **Fork-only**: no modules; one test
- **Edits to shared**: `tui_gateway/session_lifecycle.py`, `tui_gateway/session_reaper.py`
- **Guarded by**: `tests/tui_gateway/test_interrupt_system_issuer.py`
- **Upstream-watch**: `interrupt`, `issuer`

## 9. Browser-harness teardown

Browser-harness daemons are reaped on every exit path, so a headless browser cannot outlive the process
that started it (a scratch-scope leak: daemons survived for days with a `(deleted)` cwd).

- **Fork-only**: no modules; one test
- **Edits to shared**: `tools/browser_tool_lifecycle.py`
- **Guarded by**: `tests/tools/test_browser_harness_daemon_teardown.py`
- **Upstream-watch**: `browser-harness`, `daemon`, `reap`

## 10. Scratch credential shredding

The scratch dir is pruned on an idle timer; a credential-shaped file (PEM private-key header or Vault
`hvs.` token prefix) is shredded **before** the age pass, so an accidentally cached secret lives for one
prune interval instead of the full 24h idle window. Added 2026-10-01, and it had **no test** until
2026-10-16 -- which is exactly what this ledger's first verification run then reported as unlisted.

- **Fork-only**: `tests/test_scratch_credential_shred.py`
- **Edits to shared**: `hermes_constants_scratch.py`
- **Guarded by**: `tests/test_scratch_credential_shred.py` (the wiring, the helper's own count and
  idempotence, every header pattern, prefix-not-substring near misses, unreadable/binary siblings)
- **Upstream-watch**: `credential`, `_CREDENTIAL_HEADERS`, `prune_idle_entries`

---

## Maintenance

- **Adding a feature**: add a section in the same style, and add every new fork-only path to the block
  below. The probe reports anything unlisted on its next run -- that report IS the reminder.
- **Retiring a feature** (upstream built it, or we dropped it): delete the section AND the paths from the
  block. A path left in the block that no longer exists is reported as `gone` by the same check.
- **Do not put a fork-only path in the block that upstream also has.** The check compares against
  upstream directly, so an over-broad block just trades one finding for another.
- **The ledger lists ITSELF.** `FORK-CUSTOMIZATIONS.md` is fork-only too (added by us, absent
  upstream), so the probe would otherwise report it as unlisted on every single run. It is the first
  entry in the block for that reason -- not a mistake.
- **Fork-only = added on our line and absent upstream.** If a file exists in both, it belongs in the
  "Edits to shared" field of its cluster, not in the block.

## Machine-readable fork-only set

The probe reads the paths between the two markers, one repo-relative path per line. Prose above may
drift; this block is what is verified. Keep it sorted.

<!-- fork-only:start -->
FORK-CUSTOMIZATIONS.md
agent/provider_wall_notice.py
agent/quota_gate_trigger.py
cron/scheduler_quota.py
gateway/platforms/api_server_quota_gate.py
hermes_cli/cost_window.py
hermes_cli/kanban_cost_window.py
hermes_cli/quota.py
hermes_cli/quota_state.py
hermes_cli/subcommands/cost_window.py
hermes_cli/subcommands/quota.py
plugins/mobile-cli/README.md
plugins/mobile-cli/__init__.py
plugins/mobile-cli/cli.py
plugins/mobile-cli/pairing.py
plugins/mobile-cli/plugin.yaml
plugins/mobile-cli/qrterm.py
tests/agent/test_credential_pool_stale_failure_reason.py
tests/agent/test_fallback_reasoning_pin.py
tests/agent/test_overloaded_retry_budget.py
tests/agent/test_provider_wall_notice.py
tests/cron/test_cron_cost_window_release.py
tests/cron/test_cron_quota_release_all.py
tests/gateway/test_api_server_quota_gate.py
tests/gateway/test_provider_wall_notice_delivery.py
tests/hermes_cli/test_cost_window_holidays.py
tests/hermes_cli/test_kanban_assignee_namespaces.py
tests/hermes_cli/test_kanban_board_pin_scope.py
tests/hermes_cli/test_kanban_cross_fleet_assignee_gate.py
tests/hermes_cli/test_kanban_gate_holds.py
tests/hermes_cli/test_kanban_namespace_cache.py
tests/hermes_cli/test_kanban_per_profile_cap_namespaced.py
tests/hermes_cli/test_kanban_phantom_refs.py
tests/hermes_cli/test_kanban_quota_release_all.py
tests/hermes_cli/test_kanban_shared_dir_mode.py
tests/hermes_cli/test_kanban_write_guard_purge_survival.py
tests/hermes_cli/test_quota_state.py
tests/hermes_cli/test_quota_state_board.py
tests/plugins/test_mobile_cli.py
tests/test_scratch_credential_shred.py
tests/tools/test_browser_harness_daemon_teardown.py
tests/tools/test_smart_approval_degraded_notice.py
tests/tools/test_smart_approval_degraded_surfaces.py
tests/tools/test_smart_approval_offloop.py
tests/tui_gateway/test_interrupt_system_issuer.py
<!-- fork-only:end -->
