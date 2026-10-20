import { atom } from 'nanostores'

import { hermesApi } from '@/api/client'

/**
 * Whether the backend this client is attached to is running code older than the
 * checkout on disk (the post-`hermes update` skew the server reports as
 * `code_skew` on its health endpoint).
 *
 * Detected since the reconnect escalation was introduced, but never STORED: the
 * only reader was the toast, which probes once and discards the answer. That
 * left every always-visible surface blind to the one condition that needs a
 * restart — the backend pill could read "ready" indefinitely while the process
 * served stale modules, and only the Models page (whose own request refuses)
 * said anything. This atom is the missing home, so chrome can paint it without
 * a probe of its own.
 *
 * `null` = not asked yet. Consumers must render the plain (non-skew) state
 * until an answer lands, so an unprobed app never claims a fault.
 */
export const $codeSkewed = atom<boolean | null>(null)

/** The server's own `code_skew` body when it reported one, for the detail copy. */
export const $codeSkew = atom<null | { boot_rev?: string; disk_rev?: string }>(null)

/**
 * Read the backend's skew verdict from its health endpoint.
 *
 * Tries `/api/health` (serve/dashboard) then `/health` (gateway), bounded so it
 * can never delay a caller. Returns the verdict and records it, so one probe
 * serves the toast, the pill and the menus rather than each probing separately.
 */
export async function refreshCodeSkew(): Promise<boolean> {
  for (const path of ['/api/health', '/health']) {
    try {
      const body = await hermesApi<{ code_skew?: unknown }>({ method: 'GET', path, timeoutMs: 5_000 })

      if (body && typeof body === 'object' && body.code_skew) {
        const skew = body.code_skew as { boot_rev?: string; disk_rev?: string }

        $codeSkew.set(skew ?? {})
        $codeSkewed.set(true)

        return true
      }

      // A reachable, answering backend with no skew is authoritative: record the
      // clear verdict rather than leaving a stale `true` up after a restart.
      $codeSkew.set(null)
      $codeSkewed.set(false)

      return false
    } catch {
      // fall through to the next path
    }
  }

  // Nothing answered: leave the last verdict alone. A probe that could not run
  // is not evidence that the code is current.
  return $codeSkewed.get() === true
}

/** Test/reset seam — the atom is module-scoped and survives between cases. */
export function clearCodeSkew(): void {
  $codeSkew.set(null)
  $codeSkewed.set(null)
}
