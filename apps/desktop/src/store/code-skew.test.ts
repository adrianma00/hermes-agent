import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ hermesApi: vi.fn() }))

vi.mock('@/api/client', () => ({ hermesApi: mocks.hermesApi }))

import { $codeSkew, $codeSkewed, $codeSkewRespawns, $codeSkewServices, clearCodeSkew, refreshCodeSkew } from './code-skew'

// The fault this store exists for: a dashboard served `6e4d10829c` for two days
// while the checkout sat on `1729888f93c`. It answered /api/health 200 with no
// `code_skew` — its code predated that field — and the client read "answered
// without the field" as "clean", so the pill said ready while the same process
// refused model switches with "Restart required".
//
// The revision pair makes that answer decidable without the process judging
// itself: `commit` is what it booted from, `disk_commit` is the checkout now.
const STALE = '6e4d10829c8db1b4d7d19889eb724bf4480f20ad'
const CURRENT = '1729888f93cba0a30ccbfc4c67146edeaf4c6071'

/** Answer each path differently; anything unlisted rejects (endpoint absent). */
const serve = (byPath: Record<string, unknown>) =>
  mocks.hermesApi.mockImplementation(async (request: { path: string }) => {
    if (!(request.path in byPath)) {
      throw new Error('404')
    }

    return byPath[request.path]
  })

beforeEach(() => {
  mocks.hermesApi.mockReset()
  clearCodeSkew()
})

describe('the skew verdict reads revisions, not the backend’s own opinion', () => {
  it('calls a dashboard whose boot revision differs from disk skewed, and names it', async () => {
    serve({ '/api/health': { commit: STALE, disk_commit: CURRENT, ok: true } })

    expect(await refreshCodeSkew()).toBe(true)
    expect($codeSkewed.get()).toBe(true)
    expect($codeSkewServices.get()).toEqual(['dashboard'])
    // Full SHAs, clipped for copy — same shape the server's own code_skew used.
    expect($codeSkew.get()).toEqual({
      boot_rev: '6e4d10829c',
      disk_rev: '1729888f93',
      respawns: false,
      service: 'dashboard'
    })
  })

  it('calls equal revisions clean', async () => {
    serve({ '/api/health': { commit: CURRENT, disk_commit: CURRENT, ok: true } })

    expect(await refreshCodeSkew()).toBe(false)
    expect($codeSkewed.get()).toBe(false)
    expect($codeSkewServices.get()).toEqual([])
  })

  it('treats an unverifiable build as clean, exactly as before this change', async () => {
    // A build older than the revision pair answers 200 with nothing to compare.
    // It reads CLEAN, deliberately: such a build cannot start reporting the pair
    // without `hermes update`, so flagging it would be a verdict no restart can
    // clear — the same trap as a skew verdict with no retire path. The cost is
    // honest and temporary: a stale pre-pair build still reads clean.
    serve({ '/api/health': { commit: STALE, ok: true } })

    expect(await refreshCodeSkew()).toBe(false)
    expect($codeSkewed.get()).toBe(false)
    expect($codeSkew.get()).toBeNull()
    expect($codeSkewServices.get()).toEqual([])
  })

  it('treats a body with no revision fields at all as clean too', async () => {
    // What a pre-pair gateway answers: status/platform/version, nothing else.
    serve({ '/health': { platform: 'hermes-agent', status: 'ok', version: '0.22.1' } })

    expect(await refreshCodeSkew()).toBe(false)
    expect($codeSkewed.get()).toBe(false)
  })

  it('still names the skewed service when another service is unverifiable', async () => {
    // The mixed case: one service can prove skew while the other predates the
    // pair. The proof wins; the unverifiable one never masks it.
    serve({
      '/api/health': { status: 'ok', version: '0.22.1' },
      '/health': { commit: STALE, disk_commit: CURRENT }
    })

    expect(await refreshCodeSkew()).toBe(true)
    expect($codeSkewServices.get()).toEqual(['gateway'])
  })

  it('still honours the older code_skew verdict for builds that predate the pair', async () => {
    serve({
      '/api/health': { commit: STALE, code_skew: { boot_rev: '6e4d10829c', disk_rev: '1729888f93' }, ok: true }
    })

    expect(await refreshCodeSkew()).toBe(true)
    expect($codeSkew.get()).toEqual({
      boot_rev: '6e4d10829c',
      disk_rev: '1729888f93',
      respawns: false,
      service: 'dashboard'
    })
  })

  it('records that a stale dashboard expects a supervisor to restart it', async () => {
    // The backend publishes the evidence; the row uses it to say "restarting itself"
    // instead of sending the operator to the host for a process about to heal.
    serve({ '/api/health': { commit: STALE, disk_commit: CURRENT, respawn: 'marker:supervised-child' } })

    await refreshCodeSkew()

    expect($codeSkewRespawns.get()).toBe(true)
    expect($codeSkew.get()?.respawns).toBe(true)
  })

  it('treats an unknown respawn field as no promise', async () => {
    serve({ '/api/health': { commit: STALE, disk_commit: CURRENT, respawn: 'unknown' } })

    await refreshCodeSkew()

    expect($codeSkewRespawns.get()).toBe(false)
  })

  it('drops the respawn evidence once the dashboard is clean again', async () => {
    serve({ '/api/health': { commit: STALE, disk_commit: CURRENT, respawn: 'proven:5.0s' } })
    await refreshCodeSkew()
    expect($codeSkewRespawns.get()).toBe(true)

    serve({ '/api/health': { commit: CURRENT, disk_commit: CURRENT, respawn: 'proven:5.0s' } })
    await refreshCodeSkew()

    expect($codeSkewRespawns.get()).toBeNull()
  })

  it('keeps probing the second service instead of stopping at the first 200', async () => {
    // A healthy dashboard used to end the round, so a skewed gateway behind it
    // was never asked. Both are separate processes; both get a turn.
    serve({
      '/api/health': { commit: CURRENT, disk_commit: CURRENT },
      '/health': { commit: STALE, disk_commit: CURRENT }
    })

    expect(await refreshCodeSkew()).toBe(true)
    expect($codeSkewServices.get()).toEqual(['gateway'])
  })

  it('reports both services when both are stale', async () => {
    serve({
      '/api/health': { commit: STALE, disk_commit: CURRENT },
      '/health': { commit: STALE, disk_commit: CURRENT }
    })

    await refreshCodeSkew()

    expect($codeSkewServices.get()).toEqual(['dashboard', 'gateway'])
  })

  it('survives a service that is not listening at all', async () => {
    // The gateway's api_server platform is optional; its absence is not a fault.
    serve({ '/api/health': { commit: CURRENT, disk_commit: CURRENT } })

    expect(await refreshCodeSkew()).toBe(false)
    expect($codeSkewServices.get()).toEqual([])
  })

  it('leaves the last verdict alone when nothing answers', async () => {
    serve({ '/api/health': { commit: STALE, disk_commit: CURRENT } })
    await refreshCodeSkew()

    mocks.hermesApi.mockImplementation(async () => {
      throw new Error('ECONNREFUSED')
    })

    expect(await refreshCodeSkew()).toBe(true)
    expect($codeSkewed.get()).toBe(true)
  })
})

describe('clearCodeSkew', () => {
  it('drops the verdict and the service names together', async () => {
    serve({ '/api/health': { commit: STALE, disk_commit: CURRENT } })
    await refreshCodeSkew()

    clearCodeSkew()

    expect($codeSkewed.get()).toBeNull()
    expect($codeSkew.get()).toBeNull()
    expect($codeSkewServices.get()).toEqual([])
  })
})
