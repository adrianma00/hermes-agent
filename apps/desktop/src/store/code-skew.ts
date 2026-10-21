import { atom } from 'nanostores'

import { hermesApi } from '@/api/client'

/** The two processes that serve a Hermes backend, each with its own code load. */
export type BackendService = 'dashboard' | 'gateway'

export interface CodeSkewDetail {
  boot_rev?: string
  disk_rev?: string
  /** The service that reported the skew; a restart has to target THIS one. */
  service?: BackendService
}

/**
 * Whether the backend this client is attached to is running code older than the
 * checkout on disk (the post-`hermes update` skew).
 *
 * `null` = not asked yet, OR asked and unprovable. Consumers must render the
 * plain (non-skew) state for null, so an unprobed app never claims a fault.
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
}

type ServiceVerdict = { detail: CodeSkewDetail; state: 'skewed' } | { state: 'clean' | 'unknown' }

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
 * No comparable pair and no verdict is `unknown`, never `clean`. That distinction
 * is the whole point: a skewed dashboard answered exactly that way (200, and no
 * `code_skew`, which its old code could not emit) and the client read it as
 * healthy for two days while the same process refused model switches.
 */
function verdictFor(body: unknown): ServiceVerdict {
  if (!body || typeof body !== 'object') {
    return { state: 'unknown' }
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
    return { state: 'unknown' }
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
  let clean = false

  for (const probe of probes) {
    if (probe.status !== 'fulfilled') {
      continue
    }

    answered = true

    const { body, service } = probe.value
    const verdict = verdictFor(body)

    if (verdict.state === 'skewed') {
      skewed.push(service)
      detail ??= { ...verdict.detail, service }
    } else if (verdict.state === 'clean') {
      clean = true
    }
  }

  if (skewed.length > 0) {
    $codeSkewServices.set(skewed)
    $codeSkew.set(detail)
    $codeSkewed.set(true)

    return true
  }

  $codeSkewServices.set([])

  if (clean) {
    $codeSkew.set(null)
    $codeSkewed.set(false)

    return false
  }

  // Nothing answered: leave the last verdict alone. A probe that could not run is
  // not evidence that the code is current.
  if (!answered) {
    return $codeSkewed.get() === true
  }

  // Answered, but no service could prove either way — a build older than the
  // revision pair. Unknown is the honest verdict; calling this clean is the exact
  // false negative that let a skewed dashboard read "ready" indefinitely.
  $codeSkew.set(null)
  $codeSkewed.set(null)

  return false
}

/** Test/reset seam — the atoms are module-scoped and survive between cases. */
export function clearCodeSkew(): void {
  $codeSkew.set(null)
  $codeSkewed.set(null)
  $codeSkewServices.set([])
}
