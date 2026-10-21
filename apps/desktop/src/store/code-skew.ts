import { atom } from 'nanostores'

import { hermesApi } from '@/api/client'

/** The two processes that serve a Hermes backend, each with its own code load. */
export type BackendService = 'dashboard' | 'gateway'

export interface CodeSkewDetail {
  boot_rev?: string
  disk_rev?: string
  /** The service that reported the skew; a restart has to target THIS one. */
  service?: BackendService
  /** Whether that service expects a supervisor to start it again after it exits. */
  respawns?: boolean
}

/**
 * Whether the backend this client is attached to is running code older than the
 * checkout on disk (the post-`hermes update` skew).
 *
 * `null` = not asked yet. Consumers must render the plain (non-skew) state
 * until an answer lands, so an unprobed app never claims a fault.
 */
export const $codeSkewed = atom<boolean | null>(null)

/** Revisions behind the verdict, for the detail copy. */
export const $codeSkew = atom<CodeSkewDetail | null>(null)

/**
 * Which services reported a skew. One verdict per backend is not enough to act
 * on: the dashboard and the gateway are separate processes with separate code
 * loads, and restarting the wrong one changes nothing.
 */
export const $codeSkewServices = atom<BackendService[]>([])

/**
 * Whether the stale DASHBOARD expects something to restart it. The dashboard's own health
 * reports the evidence (`respawn`: a supervisor's marker, accumulated proof, or the client
 * that owns it), so a surface can say "wait, it restarts itself" instead of sending the
 * operator to run a command for a process that was about to heal on its own.
 *
 * `null` = no stale dashboard, or a build too old to publish the field.
 */
export const $codeSkewRespawns = atom<boolean | null>(null)

/** One health endpoint per service, in the order we prefer to name a culprit. */
const SERVICES: ReadonlyArray<{ path: string; service: BackendService }> = [
  { path: '/api/health', service: 'dashboard' },
  { path: '/health', service: 'gateway' }
]

const REVISION_CHARS = 10

/** Compact a full SHA for copy; anything already short is left alone. */
function shortRevision(revision: string): string {
  return revision.length > REVISION_CHARS ? revision.slice(0, REVISION_CHARS) : revision
}

interface HealthBody {
  code_skew?: unknown
  commit?: unknown
  disk_commit?: unknown
  /** Evidence that something restarts this process after it exits; 'unknown' = none. */
  respawn?: unknown
}

/** Only a positive, named evidence counts — an absent field is not a promise. */
function expectsRespawn(body: unknown): boolean {
  const evidence = (body as HealthBody | null)?.respawn

  return typeof evidence === 'string' && evidence !== 'unknown' && evidence !== ''
}

type ServiceVerdict =
  | { detail: CodeSkewDetail; state: 'skewed' }
  | { state: 'clean' | 'unverifiable' }

/**
 * Decide ONE service from its health body.
 *
 * The revision pair is authoritative when present: `commit` is the revision the
 * process booted from, `disk_commit` is the checkout on disk now, so inequality
 * IS skew — no need to trust a process to have judged itself. `code_skew` is the
 * older signal and is still honoured, because it also covers builds that predate
 * the pair; it just cannot be the ONLY signal, since a process stale enough to
 * matter may predate the field and answer 200 without it.
 *
 * A body with no comparable pair and no verdict is `unverifiable`: a build from
 * before the pair existed. It is reported as unverifiable rather than skewed, and
 * callers treat it as clean, because the honest answer is not actionable — such a
 * build cannot start reporting the pair without an `hermes update`, so a verdict
 * that never clears would nag forever with nothing to do about it. (This is the
 * same trap as a skew verdict with no retire path.) The cost is explicit: during
 * the transition, a stale pre-pair build still reads clean, exactly as it did
 * before this change. Only the payload fix removes that class for good.
 */
function verdictFor(body: unknown): ServiceVerdict {
  if (!body || typeof body !== 'object') {
    return { state: 'unverifiable' }
  }

  const health = body as HealthBody
  const legacy = health.code_skew

  if (legacy && typeof legacy === 'object') {
    const skew = legacy as { boot_rev?: unknown; disk_rev?: unknown }

    return {
      detail: {
        boot_rev: typeof skew.boot_rev === 'string' ? skew.boot_rev : undefined,
        disk_rev: typeof skew.disk_rev === 'string' ? skew.disk_rev : undefined
      },
      state: 'skewed'
    }
  }

  const boot = typeof health.commit === 'string' ? health.commit : null
  const disk = typeof health.disk_commit === 'string' ? health.disk_commit : null

  if (!boot || !disk) {
    return { state: 'unverifiable' }
  }

  if (boot === disk) {
    return { state: 'clean' }
  }

  return { detail: { boot_rev: shortRevision(boot), disk_rev: shortRevision(disk) }, state: 'skewed' }
}

/**
 * Read every service's verdict from its own health endpoint.
 *
 * Both endpoints are probed on every round — stopping at the first 200 was how
 * one stale service hid behind another's healthy answer. Bounded so it can never
 * delay a caller. Records the aggregate so one probe serves the toast, the pill
 * and the menus rather than each probing separately.
 */
export async function refreshCodeSkew(): Promise<boolean> {
  const probes = await Promise.allSettled(
    SERVICES.map(async entry => ({
      body: await hermesApi<HealthBody>({ method: 'GET', path: entry.path, timeoutMs: 5_000 }),
      service: entry.service
    }))
  )

  const skewed: BackendService[] = []
  let detail: CodeSkewDetail | null = null
  let answered = false

  for (const probe of probes) {
    if (probe.status !== 'fulfilled') {
      continue
    }

    answered = true

    const { body, service } = probe.value
    const verdict = verdictFor(body)

    if (verdict.state === 'skewed') {
      skewed.push(service)
      detail ??= { ...verdict.detail, respawns: expectsRespawn(body), service }

      if (service === 'dashboard') {
        $codeSkewRespawns.set(expectsRespawn(body))
      }
    }
  }

  if (skewed.length > 0) {
    if (!skewed.includes('dashboard')) {
      $codeSkewRespawns.set(null)
    }

    $codeSkewServices.set(skewed)
    $codeSkew.set(detail)
    $codeSkewed.set(true)

    return true
  }

  $codeSkewServices.set([])
  $codeSkewRespawns.set(null)

  // Nothing answered: leave the last verdict alone. A probe that could not run is
  // not evidence that the code is current.
  if (!answered) {
    return $codeSkewed.get() === true
  }

  // Something answered and none of it was skewed: clean. That includes a service
  // that answered `unverifiable` (a build older than the revision pair), which is
  // deliberate — the same "current behaviour" as before this change. Such a build
  // cannot begin reporting the pair without an `hermes update`, so flagging it
  // would be a warning with no remedy that only a restart cycle could not clear.
  $codeSkew.set(null)
  $codeSkewed.set(false)

  return false
}

/** Test/reset seam — the atoms are module-scoped and survive between cases. */
export function clearCodeSkew(): void {
  $codeSkew.set(null)
  $codeSkewed.set(null)
  $codeSkewServices.set([])
  $codeSkewRespawns.set(null)
}
