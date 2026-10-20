import { describe, expect, it, vi } from 'vitest'

import {
  ensureHealthyPooledRemoteBackendForDispatch,
  type EnsureHealthyPooledRemoteBackendForDispatchOptions,
  HOST_EVENT_DISTINCT_PROFILE_THRESHOLD,
  HOST_EVENT_WINDOW_MS,
  POOLED_REMOTE_DIAL_CONCURRENCY,
  POOLED_REMOTE_DIAL_JITTER_MS,
  POOLED_REMOTE_DISPATCH_PROBE_RETRY_DELAY_MS,
  POOLED_REMOTE_DISPATCH_PROBE_TIMEOUT_MS,
  PooledRemoteDialGate,
  POWER_RESUME_REVALIDATION_HOLDOFF_MS,
  probeFailureDescription,
  REMOTE_LIVENESS_FAILURE_LIMIT,
  REMOTE_LIVENESS_FAILURE_WINDOW_MS,
  REMOTE_LIVENESS_TIMEOUT_MS,
  REMOTE_POOLED_LIVENESS_FAILURE_WINDOW_MS,
  RemoteHostEventTracker,
  RemoteLivenessTracker,
  RemoteRevalidationCoordinator,
  revalidatePooledRemoteBackends,
  revalidateRemoteConnection
} from './remote-liveness'

describe('RemoteLivenessTracker', () => {
  it('requires consecutive failures before resetting a connection', () => {
    const tracker = new RemoteLivenessTracker()

    for (let failures = 1; failures < REMOTE_LIVENESS_FAILURE_LIMIT; failures += 1) {
      expect(tracker.recordFailure('https://gateway.example.com')).toEqual({ failures, shouldReset: false })
    }

    expect(tracker.recordFailure('https://gateway.example.com')).toEqual({
      failures: REMOTE_LIVENESS_FAILURE_LIMIT,
      shouldReset: true
    })
  })

  it('clears a failure streak after a successful probe', () => {
    const tracker = new RemoteLivenessTracker()

    tracker.recordFailure('https://gateway.example.com')
    tracker.recordFailure('https://gateway.example.com')
    tracker.recordSuccess('https://gateway.example.com')

    expect(tracker.recordFailure('https://gateway.example.com')).toEqual({ failures: 1, shouldReset: false })
  })

  it('tracks different gateways independently', () => {
    const tracker = new RemoteLivenessTracker(2)

    expect(tracker.recordFailure('https://one.example.com')).toEqual({ failures: 1, shouldReset: false })
    expect(tracker.recordFailure('https://two.example.com')).toEqual({ failures: 1, shouldReset: false })
    expect(tracker.recordFailure('https://one.example.com')).toEqual({ failures: 2, shouldReset: true })
    expect(tracker.recordFailure('https://two.example.com')).toEqual({ failures: 2, shouldReset: true })
  })

  it('clears only the successful gateway streak', () => {
    const tracker = new RemoteLivenessTracker(3)

    tracker.recordFailure('https://one.example.com')
    tracker.recordFailure('https://two.example.com')
    tracker.recordSuccess('https://one.example.com')

    expect(tracker.recordFailure('https://one.example.com')).toEqual({ failures: 1, shouldReset: false })
    expect(tracker.recordFailure('https://two.example.com')).toEqual({ failures: 2, shouldReset: false })
  })

  it('does not accumulate isolated failures across separate reconnect episodes', () => {
    let now = 0
    const tracker = new RemoteLivenessTracker(3, REMOTE_LIVENESS_FAILURE_WINDOW_MS, () => now)

    expect(tracker.recordFailure('https://gateway.example.com')).toEqual({ failures: 1, shouldReset: false })
    now += REMOTE_LIVENESS_FAILURE_WINDOW_MS + 1
    expect(tracker.recordFailure('https://gateway.example.com')).toEqual({ failures: 1, shouldReset: false })
  })

  it('clears all failure streaks when the connection state resets', () => {
    const tracker = new RemoteLivenessTracker(3)

    tracker.recordFailure('https://one.example.com')
    tracker.recordFailure('https://two.example.com')
    tracker.clear()

    expect(tracker.recordFailure('https://one.example.com')).toEqual({ failures: 1, shouldReset: false })
    expect(tracker.recordFailure('https://two.example.com')).toEqual({ failures: 1, shouldReset: false })
  })

  it('starts a fresh streak after the reset threshold is consumed', () => {
    const tracker = new RemoteLivenessTracker(1)

    expect(tracker.recordFailure('https://gateway.example.com')).toEqual({ failures: 1, shouldReset: true })
    expect(tracker.recordFailure('https://gateway.example.com')).toEqual({ failures: 1, shouldReset: true })
  })

  it('rejects invalid failure limits', () => {
    expect(() => new RemoteLivenessTracker(0)).toThrow(/positive integer/i)
    expect(() => new RemoteLivenessTracker(1.5)).toThrow(/positive integer/i)
    expect(() => new RemoteLivenessTracker(1, 0)).toThrow(/window must be positive/i)
  })
})

describe('RemoteRevalidationCoordinator', () => {
  it('coalesces simultaneous probes for the same cached connection', async () => {
    const coordinator = new RemoteRevalidationCoordinator()
    const connection = Promise.resolve({ baseUrl: 'https://gateway.example.com' })
    let resolveProbe: (value: string) => void = () => undefined

    const probe = vi.fn(
      () =>
        new Promise<string>(resolve => {
          resolveProbe = resolve
        })
    )

    const first = coordinator.run(connection, probe)
    const second = coordinator.run(connection, probe)
    const third = coordinator.run(connection, probe)

    await Promise.resolve()

    expect(second).toBe(first)
    expect(third).toBe(first)
    expect(probe).toHaveBeenCalledOnce()

    resolveProbe('healthy')
    await expect(Promise.all([first, second, third])).resolves.toEqual(['healthy', 'healthy', 'healthy'])
  })

  it('runs a fresh probe after the prior one settles', async () => {
    const coordinator = new RemoteRevalidationCoordinator()
    const connection = Promise.resolve({ baseUrl: 'https://gateway.example.com' })
    const probe = vi.fn().mockResolvedValue('healthy')

    await coordinator.run(connection, probe)
    await coordinator.run(connection, probe)

    expect(probe).toHaveBeenCalledTimes(2)
  })

  it('does not coalesce different cached connections', async () => {
    const coordinator = new RemoteRevalidationCoordinator()
    const probe = vi.fn().mockResolvedValue('healthy')

    await Promise.all([coordinator.run(Promise.resolve('one'), probe), coordinator.run(Promise.resolve('two'), probe)])

    expect(probe).toHaveBeenCalledTimes(2)
  })

  it('cleans up a rejected probe so it can be retried', async () => {
    const coordinator = new RemoteRevalidationCoordinator()
    const connection = Promise.resolve({ baseUrl: 'https://gateway.example.com' })
    const probe = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce('healthy')

    await expect(coordinator.run(connection, probe)).rejects.toThrow('offline')
    await expect(coordinator.run(connection, probe)).resolves.toBe('healthy')
    expect(probe).toHaveBeenCalledTimes(2)
  })
})

describe('revalidateRemoteConnection', () => {
  function harness(overrides: Record<string, unknown> = {}) {
    const connection = { baseUrl: 'https://gateway.example.com/', mode: 'remote' }
    const connectionPromise = Promise.resolve(connection)
    const current = { promise: connectionPromise as null | Promise<typeof connection> }
    const log = vi.fn()
    const probe = vi.fn().mockResolvedValue({ ok: true })
    const resetConnection = vi.fn()
    const tracker = new RemoteLivenessTracker()

    return {
      connectionPromise,
      current,
      log,
      options: {
        connectionPromise,
        currentConnectionPromise: () => current.promise,
        log,
        probe,
        resetConnection,
        tracker,
        ...overrides
      },
      probe,
      resetConnection,
      tracker
    }
  }

  it('probes the normalized status URL with the production timeout', async () => {
    const test = harness()

    await expect(revalidateRemoteConnection(test.options)).resolves.toEqual({ ok: true, rebuilt: false })
    expect(test.probe).toHaveBeenCalledWith(
      expect.objectContaining({ baseUrl: 'https://gateway.example.com/' }),
      '/api/status',
      {
        timeoutMs: REMOTE_LIVENESS_TIMEOUT_MS
      }
    )
    expect(test.resetConnection).not.toHaveBeenCalled()
  })

  it('keeps failures one and two, then resets on the third failure', async () => {
    const probe = vi.fn().mockRejectedValue(new Error('offline'))
    const test = harness({ probe })

    await expect(revalidateRemoteConnection(test.options)).resolves.toEqual({ ok: true, rebuilt: false })
    await expect(revalidateRemoteConnection(test.options)).resolves.toEqual({ ok: true, rebuilt: false })
    await expect(revalidateRemoteConnection(test.options)).resolves.toEqual({ ok: true, rebuilt: true })

    expect(probe).toHaveBeenCalledTimes(3)
    expect(test.resetConnection).toHaveBeenCalledOnce()
    expect(test.log).toHaveBeenNthCalledWith(1, expect.stringContaining('(1/3)'))
    expect(test.log).toHaveBeenNthCalledWith(2, expect.stringContaining('(2/3)'))
    expect(test.log).toHaveBeenLastCalledWith(expect.stringContaining('dropping stale connection'))
  })

  it('ignores a late failed probe after the cached connection is replaced', async () => {
    let rejectProbe: (error: Error) => void = () => undefined

    const probe = vi.fn(
      () =>
        new Promise((_resolve, reject) => {
          rejectProbe = reject
        })
    )

    const test = harness({ probe })
    const pending = revalidateRemoteConnection(test.options)

    await Promise.resolve()
    test.current.promise = Promise.resolve({ baseUrl: 'https://new.example.com', mode: 'remote' })
    rejectProbe(new Error('old connection failed'))

    await expect(pending).resolves.toEqual({ ok: true, rebuilt: false })
    expect(test.resetConnection).not.toHaveBeenCalled()
    expect(test.log).not.toHaveBeenCalled()
    expect(test.tracker.recordFailure('https://gateway.example.com')).toEqual({ failures: 1, shouldReset: false })
  })

  it('does not probe a local, rejected, or already replaced connection', async () => {
    const replaced = harness()

    replaced.current.promise = null
    await expect(revalidateRemoteConnection(replaced.options)).resolves.toEqual({ ok: true, rebuilt: false })
    expect(replaced.probe).not.toHaveBeenCalled()

    const localConnection = { baseUrl: 'http://127.0.0.1:3000', mode: 'local' }
    const localPromise = Promise.resolve(localConnection)

    const local = harness({
      connectionPromise: localPromise,
      currentConnectionPromise: () => localPromise
    })

    await expect(revalidateRemoteConnection(local.options)).resolves.toEqual({ ok: true, rebuilt: false })
    expect(local.probe).not.toHaveBeenCalled()

    const rejectedPromise = Promise.reject(new Error('boot failed'))

    const rejected = harness({
      connectionPromise: rejectedPromise,
      currentConnectionPromise: () => rejectedPromise
    })

    await expect(revalidateRemoteConnection(rejected.options)).resolves.toEqual({ ok: true, rebuilt: false })
    expect(rejected.probe).not.toHaveBeenCalled()
  })

  // The primary connection has no pool sibling to vote with, so it cannot use
  // the pooled host-event signal. A busy host still makes its quick streak
  // expire, and dropping it is a visible whole-app reload.
  describe('drop confirmation', () => {
    function confirmation(overrides: Record<string, unknown> = {}) {
      return {
        backoff: vi.fn(async () => undefined),
        transportAlive: vi.fn(async () => true),
        ...overrides
      }
    }

    it('keeps a connection that answers the re-probe after the backoff', async () => {
      const probe = vi
        .fn()
        .mockRejectedValueOnce(new Error('ECONNRESET'))
        .mockRejectedValueOnce(new Error('ECONNRESET'))
        .mockRejectedValueOnce(new Error('ECONNRESET'))
        .mockResolvedValueOnce({ ok: true })

      const confirmDrop = confirmation()
      const test = harness({ confirmDrop, probe })

      await expect(revalidateRemoteConnection(test.options)).resolves.toEqual({ ok: true, rebuilt: false })
      await expect(revalidateRemoteConnection(test.options)).resolves.toEqual({ ok: true, rebuilt: false })
      await expect(revalidateRemoteConnection(test.options)).resolves.toEqual({ ok: true, rebuilt: false })

      expect(confirmDrop.backoff).toHaveBeenCalledOnce()
      expect(confirmDrop.transportAlive).toHaveBeenCalledOnce()
      expect(probe).toHaveBeenCalledTimes(4)
      expect(test.resetConnection).not.toHaveBeenCalled()
      expect(test.log).toHaveBeenLastCalledWith(expect.stringContaining('answered after'))
    })

    it('still drops a connection whose re-probe fails after the backoff', async () => {
      const probe = vi.fn().mockRejectedValue(new Error('ECONNRESET'))
      const confirmDrop = confirmation()
      const test = harness({ confirmDrop, probe })

      await revalidateRemoteConnection(test.options)
      await revalidateRemoteConnection(test.options)
      await expect(revalidateRemoteConnection(test.options)).resolves.toEqual({ ok: true, rebuilt: true })

      expect(confirmDrop.backoff).toHaveBeenCalledOnce()
      expect(probe).toHaveBeenCalledTimes(4)
      expect(test.resetConnection).toHaveBeenCalledOnce()
      expect(test.log).toHaveBeenLastCalledWith(expect.stringContaining('dropping stale connection'))
    })

    it('drops without a re-probe when the transport itself is gone', async () => {
      const probe = vi.fn().mockRejectedValue(new Error('ECONNRESET'))
      const confirmDrop = confirmation({ transportAlive: vi.fn(async () => false) })
      const test = harness({ confirmDrop, probe })

      await revalidateRemoteConnection(test.options)
      await revalidateRemoteConnection(test.options)
      await expect(revalidateRemoteConnection(test.options)).resolves.toEqual({ ok: true, rebuilt: true })

      // Three liveness probes and no fourth: a dead transport needs no re-probe.
      expect(probe).toHaveBeenCalledTimes(3)
      expect(test.resetConnection).toHaveBeenCalledOnce()
    })

    it('drops on the unchanged fast path when no confirmation is wired', async () => {
      const probe = vi.fn().mockRejectedValue(new Error('offline'))
      const test = harness({ probe })

      await revalidateRemoteConnection(test.options)
      await revalidateRemoteConnection(test.options)
      await expect(revalidateRemoteConnection(test.options)).resolves.toEqual({ ok: true, rebuilt: true })

      expect(probe).toHaveBeenCalledTimes(3)
      expect(test.resetConnection).toHaveBeenCalledOnce()
    })
  })
})

describe('ensureHealthyPooledRemoteBackendForDispatch', () => {
  it('covers quiet-box cold-start and stays below the power-resume holdoff', () => {
    expect(POOLED_REMOTE_DISPATCH_PROBE_TIMEOUT_MS).toBeGreaterThanOrEqual(REMOTE_LIVENESS_TIMEOUT_MS)
    expect(POOLED_REMOTE_DISPATCH_PROBE_TIMEOUT_MS).toBeGreaterThanOrEqual(8_000)
    expect(POWER_RESUME_REVALIDATION_HOLDOFF_MS).toBeGreaterThan(POOLED_REMOTE_DISPATCH_PROBE_TIMEOUT_MS)
  })

  it('returns a healthy cached descriptor without retiring or reconnecting', async () => {
    const healthy = { baseUrl: 'http://127.0.0.1:49525', mode: 'remote' }
    const connectionPromise = Promise.resolve(healthy)
    const retire = vi.fn()
    const reconnect = vi.fn()
    const probe = vi.fn().mockResolvedValue({ ok: true })

    await expect(
      ensureHealthyPooledRemoteBackendForDispatch({
        connectionPromise,
        currentConnectionPromise: () => connectionPromise,
        probe,
        reconnect,
        retire,
        tracker: new RemoteLivenessTracker()
      })
    ).resolves.toBe(healthy)

    expect(probe).toHaveBeenCalledWith(healthy, '/api/health', {
      timeoutMs: POOLED_REMOTE_DISPATCH_PROBE_TIMEOUT_MS
    })
    expect(retire).not.toHaveBeenCalled()
    expect(reconnect).not.toHaveBeenCalled()
  })

  it('retires a dead cached descriptor and gives dispatch the replacement', async () => {
    const stale = { baseUrl: 'http://127.0.0.1:49525', mode: 'remote' }
    const replacement = { baseUrl: 'http://127.0.0.1:53968', mode: 'remote' }
    const stalePromise = Promise.resolve(stale)
    let currentPromise: Promise<typeof stale> | null = stalePromise

    const retire = vi.fn(async () => {
      currentPromise = null
    })

    const reconnect = vi.fn(async () => {
      currentPromise = Promise.resolve(replacement)

      return replacement
    })

    const probe = vi.fn(async connection => {
      if (connection === stale) {
        throw new Error('connect ECONNREFUSED 127.0.0.1:49525')
      }
    })

    await expect(
      ensureHealthyPooledRemoteBackendForDispatch({
        connectionPromise: stalePromise,
        currentConnectionPromise: () => currentPromise,
        probe,
        reconnect,
        retire,
        tracker: new RemoteLivenessTracker(1)
      })
    ).resolves.toBe(replacement)

    expect(probe).toHaveBeenCalledWith(stale, '/api/health', {
      timeoutMs: POOLED_REMOTE_DISPATCH_PROBE_TIMEOUT_MS
    })
    expect(retire).toHaveBeenCalledOnce()
    expect(reconnect).toHaveBeenCalledOnce()
  })

  it('falls back to /api/status when /api/health returns 404 on older backends', async () => {
    const legacy = { baseUrl: 'http://127.0.0.1:49525', mode: 'remote' }
    const legacyPromise = Promise.resolve(legacy)

    const retire = vi.fn()
    const reconnect = vi.fn()

    const probe = vi.fn(async (_connection, path) => {
      if (path === '/api/health') {
        throw new Error('404: Not Found')
      }
    })

    await expect(
      ensureHealthyPooledRemoteBackendForDispatch({
        connectionPromise: legacyPromise,
        currentConnectionPromise: () => legacyPromise,
        probe,
        reconnect,
        retire,
        tracker: new RemoteLivenessTracker()
      })
    ).resolves.toBe(legacy)

    expect(probe).toHaveBeenCalledWith(legacy, '/api/status', {
      timeoutMs: POOLED_REMOTE_DISPATCH_PROBE_TIMEOUT_MS
    })
    expect(retire).not.toHaveBeenCalled()
    expect(reconnect).not.toHaveBeenCalled()
  })

  it('keeps a healthy descriptor when one transient hang-up clears on the confirmation probe (#131765)', async () => {
    const healthy = { baseUrl: 'http://127.0.0.1:49525', mode: 'remote' }
    const healthyPromise = Promise.resolve(healthy)
    const retire = vi.fn()
    const reconnect = vi.fn()
    const log = vi.fn()

    // The reporter's shape: the forward is bound and answering (a direct
    // curl gets 200), but the pooled dispatch probe sees one socket hang up.
    const hangUp = new Error('socket hang up') as Error & { code: string }
    hangUp.code = 'ECONNRESET'

    const probe = vi
      .fn<EnsureHealthyPooledRemoteBackendForDispatchOptions<unknown>['probe']>()
      .mockRejectedValueOnce(hangUp)
      .mockResolvedValueOnce({ ok: true })

    await expect(
      ensureHealthyPooledRemoteBackendForDispatch({
        connectionPromise: healthyPromise,
        currentConnectionPromise: () => healthyPromise,
        probe,
        reconnect,
        retire,
        log,
        retryDelayMs: 0,
        tracker: new RemoteLivenessTracker()
      })
    ).resolves.toBe(healthy)

    expect(probe).toHaveBeenCalledTimes(2)
    expect(retire).not.toHaveBeenCalled()
    expect(reconnect).not.toHaveBeenCalled()
    expect(log).toHaveBeenCalledWith(
      expect.stringContaining('recovered after one retry')
    )
    expect(log).toHaveBeenCalledWith(expect.stringContaining('errno ECONNRESET'))
  })

  it('retires the descriptor only when the confirmation probe fails too', async () => {
    const stale = { baseUrl: 'http://127.0.0.1:49525', mode: 'remote' }
    const replacement = { baseUrl: 'http://127.0.0.1:53968', mode: 'remote' }
    const stalePromise = Promise.resolve(stale)
    let currentPromise: Promise<typeof stale> | null = stalePromise
    const log = vi.fn()

    const retire = vi.fn(async () => {
      currentPromise = null
    })

    const reconnect = vi.fn(async () => {
      currentPromise = Promise.resolve(replacement)

      return replacement
    })

    const probe = vi
      .fn<EnsureHealthyPooledRemoteBackendForDispatchOptions<unknown>['probe']>()
      .mockRejectedValue(new Error('connect ECONNREFUSED 127.0.0.1:49525'))

    await expect(
      ensureHealthyPooledRemoteBackendForDispatch({
        connectionPromise: stalePromise,
        currentConnectionPromise: () => currentPromise,
        probe,
        reconnect,
        retire,
        log,
        retryDelayMs: 0,
        tracker: new RemoteLivenessTracker(1)
      })
    ).resolves.toBe(replacement)

    // One failure re-verified before death, and the streak limit reached on
    // that second round: two probe rounds, then retire.
    expect(probe.mock.calls.filter(([connection]) => connection === stale)).toHaveLength(2)
    expect(retire).toHaveBeenCalledOnce()
    expect(reconnect).toHaveBeenCalledOnce()
    expect(log).toHaveBeenCalledWith(expect.stringContaining('failed twice'))
  })

  it('does not touch a descriptor replaced during the retry backoff', async () => {
    const stale = { baseUrl: 'http://127.0.0.1:49525', mode: 'remote' }
    const replacement = { baseUrl: 'http://127.0.0.1:53968', mode: 'remote' }
    const stalePromise = Promise.resolve(stale)
    let currentPromise: Promise<typeof stale> | null = stalePromise
    const retire = vi.fn()
    const log = vi.fn()

    const probe = vi
      .fn<EnsureHealthyPooledRemoteBackendForDispatchOptions<unknown>['probe']>()
      .mockImplementationOnce(() => {
        currentPromise = Promise.resolve(replacement)

        return Promise.reject(new Error('socket hang up'))
      })

    await expect(
      ensureHealthyPooledRemoteBackendForDispatch({
        connectionPromise: stalePromise,
        currentConnectionPromise: () => currentPromise,
        probe,
        reconnect: () => Promise.resolve(replacement),
        retire,
        log,
        retryDelayMs: 0,
        tracker: new RemoteLivenessTracker()
      })
    ).resolves.toBe(replacement)

    expect(probe).toHaveBeenCalledTimes(1)
    expect(retire).not.toHaveBeenCalled()
    expect(log).not.toHaveBeenCalled()
  })

  it('bypasses a busy descriptor below the streak limit, retiring only on the third round (#131765)', async () => {
    // The observed shape: a live bridge starves past the probe budget twice, and the old gate
    // retired it - tearing down the forward and killing the running turn. Retirement is now
    // streak-gated on the same tracker the background revalidation uses.
    const stale = { baseUrl: 'http://127.0.0.1:49525', mode: 'remote' }
    const stalePromise = Promise.resolve(stale)
    const tracker = new RemoteLivenessTracker()
    const log = vi.fn()
    const retire = vi.fn()
    const reconnect = vi.fn(async () => stale)
    const probe = vi
      .fn<EnsureHealthyPooledRemoteBackendForDispatchOptions<unknown>['probe']>()
      .mockRejectedValue(new Error('Timed out connecting to Hermes backend after 12000ms'))

    const run = () =>
      ensureHealthyPooledRemoteBackendForDispatch({
        connectionPromise: stalePromise,
        currentConnectionPromise: () => stalePromise,
        probe,
        reconnect,
        retire,
        log,
        retryDelayMs: 0,
        tracker
      })

    await expect(run()).resolves.toBe(stale)
    expect(retire).not.toHaveBeenCalled()
    // A bypass hands dispatch the cached descriptor; it never re-dials (a
    // re-entrant dial would await the run calling it - see the self-await test).
    expect(reconnect).not.toHaveBeenCalled()
    expect(log).toHaveBeenCalledWith(expect.stringContaining('keeping descriptor for retry (1/3)'))
    // Attribution: the line must name which connection stalled, so the next incident does not
    // need ssh-argv forensics to identify the dead forward.
    expect(log).toHaveBeenCalledWith(expect.stringContaining('for http://127.0.0.1:49525'))

    await expect(run()).resolves.toBe(stale)
    expect(retire).not.toHaveBeenCalled()
    expect(log).toHaveBeenCalledWith(expect.stringContaining('keeping descriptor for retry (2/3)'))

    await expect(run()).resolves.toBe(stale)
    expect(retire).toHaveBeenCalledOnce()
    expect(reconnect).toHaveBeenCalledOnce()
    expect(log).toHaveBeenCalledWith(expect.stringContaining('retiring descriptor'))
  })

  it('retires a stalled descriptor whose remote dashboard has exited, instead of bypassing', async () => {
    // A bound local forward keeps a dead far end looking like a stall, so the
    // streak policy would read (1/3) and keep the descriptor - the route then
    // stays on a dead backend until relaunch. A proven-gone pid is not a busy
    // bridge.
    const stale = { baseUrl: 'http://127.0.0.1:49525', mode: 'remote' }
    const replacement = { baseUrl: 'http://127.0.0.1:53968', mode: 'remote' }
    const stalePromise = Promise.resolve(stale)
    let currentPromise: Promise<typeof stale> | null = stalePromise
    const log = vi.fn()
    const retire = vi.fn(async () => {
      currentPromise = null
    })

    const reconnect = vi.fn(async () => {
      currentPromise = Promise.resolve(replacement)

      return replacement
    })

    const probe = vi
      .fn<EnsureHealthyPooledRemoteBackendForDispatchOptions<unknown>['probe']>()
      .mockRejectedValue(new Error('Timed out connecting to Hermes backend after 12000ms'))
    const remoteDashboardAlive = vi.fn(async () => false)

    await expect(
      ensureHealthyPooledRemoteBackendForDispatch({
        connectionPromise: stalePromise,
        currentConnectionPromise: () => currentPromise,
        probe,
        reconnect,
        retire,
        log,
        remoteDashboardAlive,
        retryDelayMs: 0,
        tracker: new RemoteLivenessTracker()
      })
    ).resolves.toBe(replacement)

    expect(remoteDashboardAlive).toHaveBeenCalledOnce()
    expect(retire).toHaveBeenCalledOnce()
    expect(reconnect).toHaveBeenCalledOnce()
    expect(log).toHaveBeenCalledWith(
      expect.stringContaining('retiring descriptor (remote dashboard is gone)')
    )
    expect(log).not.toHaveBeenCalledWith(expect.stringContaining('keeping descriptor for retry'))
  })

  it('keeps a stalled descriptor when the far end is alive or its proof cannot settle', async () => {
    const shape = (
      remoteDashboardAlive: () => Promise<boolean>
    ) => {
      const stale = { baseUrl: 'http://127.0.0.1:49525', mode: 'remote' }
      const stalePromise = Promise.resolve(stale)
      const log = vi.fn()
      const retire = vi.fn()
      const probe = vi
        .fn<EnsureHealthyPooledRemoteBackendForDispatchOptions<unknown>['probe']>()
        .mockRejectedValue(new Error('Timed out connecting to Hermes backend after 12000ms'))

      return {
        log,
        options: {
          connectionPromise: stalePromise,
          currentConnectionPromise: () => stalePromise,
          probe,
          reconnect: vi.fn(),
          retire,
          log,
          remoteDashboardAlive,
          retryDelayMs: 0,
          tracker: new RemoteLivenessTracker()
        },
        retire,
        stale
      }
    }

    const alive = shape(async () => true)

    await expect(ensureHealthyPooledRemoteBackendForDispatch(alive.options)).resolves.toBe(alive.stale)
    expect(alive.retire).not.toHaveBeenCalled()
    expect(alive.log).toHaveBeenCalledWith(expect.stringContaining('keeping descriptor for retry (1/3)'))

    // An unsettled ownership probe must never be read as "gone".
    const unsettled = shape(async () => {
      throw new Error('Could not verify the SSH backend process.')
    })

    await expect(ensureHealthyPooledRemoteBackendForDispatch(unsettled.options)).resolves.toBe(
      unsettled.stale
    )
    expect(unsettled.retire).not.toHaveBeenCalled()
    expect(unsettled.log).toHaveBeenCalledWith(
      expect.stringContaining('keeping descriptor for retry (1/3)')
    )
  })

  it('never re-enters the dial from the bypass: a re-entrant dial self-awaits the in-flight run', async () => {
    // The caller wraps this gate in RemoteRevalidationCoordinator keyed by the
    // cached promise, so a keep path that called reconnect() -> the same ensure
    // path would be handed its own in-flight run and await itself forever -
    // the app stays offline with no failed dispatch to advance the streak.
    const stale = { baseUrl: 'http://127.0.0.1:49525', mode: 'remote' }
    const stalePromise = Promise.resolve(stale)
    const coordinator = new RemoteRevalidationCoordinator()
    const probe = vi
      .fn<EnsureHealthyPooledRemoteBackendForDispatchOptions<unknown>['probe']>()
      .mockRejectedValue(new Error('Timed out connecting to Hermes backend after 12000ms'))

    const dial = (): Promise<typeof stale> =>
      coordinator.run(stalePromise, () =>
        ensureHealthyPooledRemoteBackendForDispatch({
          connectionPromise: stalePromise,
          currentConnectionPromise: () => stalePromise,
          probe,
          reconnect: () => dial(),
          retire: vi.fn(),
          retryDelayMs: 0,
          tracker: new RemoteLivenessTracker()
        })
      )

    await expect(dial()).resolves.toBe(stale)
  })

  it('clears the dispatch streak after a successful probe', async () => {
    const stale = { baseUrl: 'http://127.0.0.1:49525', mode: 'remote' }
    const stalePromise = Promise.resolve(stale)
    const tracker = new RemoteLivenessTracker()
    const log = vi.fn()
    const probe = vi
      .fn<EnsureHealthyPooledRemoteBackendForDispatchOptions<unknown>['probe']>()
      .mockRejectedValueOnce(new Error('socket hang up'))
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(new Error('socket hang up'))
      .mockRejectedValueOnce(new Error('socket hang up'))

    const run = () =>
      ensureHealthyPooledRemoteBackendForDispatch({
        connectionPromise: stalePromise,
        currentConnectionPromise: () => stalePromise,
        probe,
        reconnect: vi.fn(async () => stale),
        retire: vi.fn(),
        log,
        retryDelayMs: 0,
        tracker
      })

    // Round 1: one blip, then the confirmation succeeds - the streak clears.
    await run()
    expect(log).toHaveBeenCalledWith(expect.stringContaining('recovered after one retry'))

    // Round 2: a double failure must read 1/3, not 2/3.
    await run()
    expect(log).toHaveBeenCalledWith(expect.stringContaining('keeping descriptor for retry (1/3)'))
  })

  it('retires immediately when the transport is gone, instead of bypassing (#131765)', async () => {
    // A reset far end is not a live-but-busy bridge. Deferring this retire left the app offline
    // with its route pointed at a dead backend, because no dispatch ever advanced the streak.
    const stale = { baseUrl: 'http://127.0.0.1:49525', mode: 'remote' }
    const stalePromise = Promise.resolve(stale)
    const tracker = new RemoteLivenessTracker()
    const log = vi.fn()
    const retire = vi.fn()
    const probe = vi
      .fn<EnsureHealthyPooledRemoteBackendForDispatchOptions<unknown>['probe']>()
      .mockRejectedValue(Object.assign(new Error('read ECONNRESET'), { code: 'ECONNRESET' }))

    await expect(
      ensureHealthyPooledRemoteBackendForDispatch({
        connectionPromise: stalePromise,
        currentConnectionPromise: () => stalePromise,
        probe,
        reconnect: vi.fn(async () => stale),
        retire,
        log,
        retryDelayMs: 0,
        tracker
      })
    ).resolves.toBe(stale)

    expect(retire).toHaveBeenCalledOnce()
    expect(log).toHaveBeenCalledWith(expect.stringContaining('retiring descriptor'))
    expect(log).not.toHaveBeenCalledWith(expect.stringContaining('keeping descriptor for retry'))
  })

  it('keeps the retry bounded relative to the power-resume holdoff', () => {
    expect(POOLED_REMOTE_DISPATCH_PROBE_RETRY_DELAY_MS).toBeGreaterThan(0)
    expect(POOLED_REMOTE_DISPATCH_PROBE_RETRY_DELAY_MS).toBeLessThan(5_000)
    // The confirmation round is bounded by the probe timeout, and the pair
    // (probe + backoff) must stay under the resume holdoff so a wake sweep can
    // never queue behind a dispatch gate. The probe timeout already exceeds
    // the holdoff, so the retry's own budget is what has to fit here.
    expect(
      POOLED_REMOTE_DISPATCH_PROBE_TIMEOUT_MS + POOLED_REMOTE_DISPATCH_PROBE_RETRY_DELAY_MS
    ).toBeLessThan(POWER_RESUME_REVALIDATION_HOLDOFF_MS)
  })

  it('describes a probe failure with its errno and cause, not only the message', () => {
    const cause = new Error('read ECONNRESET')
    const error = new Error('socket hang up') as Error & { cause: Error; code: string }
    error.cause = cause
    error.code = 'ECONNRESET'

    const described = probeFailureDescription(error)

    expect(described).toContain('socket hang up')
    expect(described).toContain('errno ECONNRESET')
    expect(described).toContain('cause: read ECONNRESET')

    // Deduplicated across the pair and tolerant of non-Error values.
    expect(probeFailureDescription(error, error)).toBe('socket hang up; cause: read ECONNRESET; errno ECONNRESET')
    expect(probeFailureDescription('raw failure')).toBe('raw failure')
    expect(probeFailureDescription()).toBe('')
  })

  describe('host events', () => {
    // One busy host, many pooled profiles: the harness models a whole
    // connection so a probe failure can be correlated the way the real pool
    // sees it, instead of one descriptor in isolation.
    const hostHarness = () => {
      const tracker = new RemoteHostEventTracker()
      const state = { backendAnswers: true, backoffs: 0, hostReachable: true, probeFails: true }

      const probe = vi.fn(async () => {
        if (state.probeFails) {
          throw new Error('read ECONNRESET')
        }
      })

      const dispatch = (poolKey: string, connection: { baseUrl: string; mode: string }) => {
        const connectionPromise = Promise.resolve(connection)
        const retire = vi.fn(async () => {})
        const reconnect = vi.fn(async () => connection)
        const livenessTracker = new RemoteLivenessTracker()

        const result = ensureHealthyPooledRemoteBackendForDispatch({
          connectionPromise,
          currentConnectionPromise: () => connectionPromise,
          hostEvent: {
            backoff: async () => {
              state.backoffs += 1
              // The backoff is where a busy host recovers; model that by
              // letting the descriptor answer again once it has elapsed.
              state.probeFails = !state.backendAnswers
            },
            classify: () => tracker.recordProbeFailure('conn:remote', poolKey),
            hostAlive: async () => state.hostReachable
          },
          probe,
          reconnect,
          retire,
          tracker: livenessTracker
        })

        return { connection, reconnect, result, retire }
      }

      return { dispatch, probe, state, tracker }
    }

    const descriptorFor = (poolKey: string) => ({ baseUrl: `http://127.0.0.1/${poolKey}`, mode: 'remote' })

    it('defers teardown once distinct profiles fail together and keeps a descriptor that recovers', async () => {
      const host = hostHarness()
      const keys = ['conn:remote::alpha', 'conn:remote::beta', 'conn:remote::gamma']

      const dispatches = keys.map(key => host.dispatch(key, descriptorFor(key)))

      await Promise.all(dispatches.map(dispatch => dispatch.result))

      // The first two are below the distinct-profile threshold, so they take
      // the unchanged fast path; the third crosses it and must be spared.
      expect(dispatches[2].retire).not.toHaveBeenCalled()
      expect(dispatches[2].reconnect).not.toHaveBeenCalled()
      await expect(dispatches[2].result).resolves.toBe(dispatches[2].connection)
      expect(host.state.backoffs).toBe(1)
    })

    it('still retires a descriptor that is dead after the host-event backoff', async () => {
      const host = hostHarness()
      host.state.backendAnswers = false
      const keys = ['conn:remote::alpha', 'conn:remote::beta', 'conn:remote::gamma']

      const dispatches = keys.map(key => host.dispatch(key, descriptorFor(key)))

      await Promise.all(dispatches.map(dispatch => dispatch.result))

      expect(host.state.backoffs).toBe(1)
      expect(dispatches[2].retire).toHaveBeenCalledOnce()
      expect(dispatches[2].reconnect).toHaveBeenCalledOnce()
    })

    it('retires when the host itself is still unreachable after the backoff', async () => {
      const host = hostHarness()
      host.state.hostReachable = false
      host.state.backendAnswers = true
      const keys = ['conn:remote::alpha', 'conn:remote::beta', 'conn:remote::gamma']

      const dispatches = keys.map(key => host.dispatch(key, descriptorFor(key)))

      await Promise.all(dispatches.map(dispatch => dispatch.result))

      expect(dispatches[2].retire).toHaveBeenCalledOnce()
    })

    it('retires a lone failing profile immediately, with no backoff', async () => {
      const host = hostHarness()
      const solo = host.dispatch('conn:remote::alpha', descriptorFor('alpha'))

      await solo.result

      expect(host.state.backoffs).toBe(0)
      expect(solo.retire).toHaveBeenCalledOnce()
      expect(solo.reconnect).toHaveBeenCalledOnce()
    })
  })
})

describe('RemoteHostEventTracker', () => {
  it('classifies a host event only after enough DISTINCT profiles fail in the window', () => {
    const tracker = new RemoteHostEventTracker()

    expect(tracker.recordProbeFailure('conn:a', 'conn:a::one')).toBe(false)
    expect(tracker.recordProbeFailure('conn:a', 'conn:a::two')).toBe(false)
    expect(tracker.recordProbeFailure('conn:a', 'conn:a::three')).toBe(true)
    expect(tracker.recordProbeFailure('conn:a', 'conn:a::four')).toBe(true)
  })

  it('never classifies one profile failing repeatedly as a host event', () => {
    const tracker = new RemoteHostEventTracker()

    for (let attempt = 0; attempt < HOST_EVENT_DISTINCT_PROFILE_THRESHOLD + 2; attempt += 1) {
      expect(tracker.recordProbeFailure('conn:a', 'conn:a::one')).toBe(false)
    }
  })

  it('tracks connections independently', () => {
    const tracker = new RemoteHostEventTracker()

    tracker.recordProbeFailure('conn:a', 'conn:a::one')
    tracker.recordProbeFailure('conn:a', 'conn:a::two')

    expect(tracker.recordProbeFailure('conn:b', 'conn:b::one')).toBe(false)
    expect(tracker.recordProbeFailure('conn:a', 'conn:a::three')).toBe(true)
  })

  it('ages failures out of the window instead of latching', () => {
    let now = 0
    const tracker = new RemoteHostEventTracker(HOST_EVENT_DISTINCT_PROFILE_THRESHOLD, HOST_EVENT_WINDOW_MS, () => now)

    expect(tracker.recordProbeFailure('conn:a', 'conn:a::one')).toBe(false)
    expect(tracker.recordProbeFailure('conn:a', 'conn:a::two')).toBe(false)

    now += HOST_EVENT_WINDOW_MS + 1

    // The earlier two are stale, so an isolated later failure is not a herd.
    expect(tracker.recordProbeFailure('conn:a', 'conn:a::three')).toBe(false)
  })

  it('rejects a threshold that could fire on a single profile', () => {
    expect(() => new RemoteHostEventTracker(1)).toThrow(/at least 2/)
  })
})

describe('PooledRemoteDialGate', () => {
  const gateHarness = (limit = POOLED_REMOTE_DIAL_CONCURRENCY) => {
    const delays: number[] = []
    const releases: Array<() => void> = []
    let concurrent = 0
    let peak = 0

    const gate = new PooledRemoteDialGate({
      delay: async ms => {
        delays.push(ms)
      },
      jitterMs: POOLED_REMOTE_DIAL_JITTER_MS,
      limit,
      random: () => 0.5
    })

    const dial = (connectionId: string) => {
      const result = gate.run(connectionId, async () => {
        concurrent += 1
        peak = Math.max(peak, concurrent)

        await new Promise<void>(resolve => {
          releases.push(() => {
            concurrent -= 1
            resolve()
          })
        })

        return connectionId
      })

      return result
    }

    return {
      delays,
      dial,
      gate,
      peak: () => peak,
      releaseAll: async () => {
        while (releases.length > 0) {
          releases.shift()?.()
          // Let the released slot's waiter be admitted before the next round.
          await new Promise<void>(resolve => setTimeout(resolve, 0))
        }
      }
    }
  }

  it('never lets one connection exceed the dial concurrency cap', async () => {
    const harness = gateHarness()
    const dials = Array.from({ length: 21 }, () => harness.dial('conn:remote'))

    await new Promise<void>(resolve => setTimeout(resolve, 0))

    expect(harness.gate.active('conn:remote')).toBe(POOLED_REMOTE_DIAL_CONCURRENCY)

    await harness.releaseAll()
    await Promise.all(dials)

    expect(harness.peak()).toBe(POOLED_REMOTE_DIAL_CONCURRENCY)
  })

  it('jitters every queued admission but never the first ones', async () => {
    const harness = gateHarness()
    const dials = Array.from({ length: 5 }, () => harness.dial('conn:remote'))

    await new Promise<void>(resolve => setTimeout(resolve, 0))

    expect(harness.delays).toEqual([])

    await harness.releaseAll()
    await Promise.all(dials)

    // Two callers were queued, so exactly two admissions paid jitter.
    expect(harness.delays).toEqual([POOLED_REMOTE_DIAL_JITTER_MS / 2, POOLED_REMOTE_DIAL_JITTER_MS / 2])
  })

  it('does not make one connection wait behind another', async () => {
    const harness = gateHarness(1)
    const first = harness.dial('conn:one')
    const second = harness.dial('conn:two')

    await new Promise<void>(resolve => setTimeout(resolve, 0))

    expect(harness.peak()).toBe(2)

    await harness.releaseAll()
    await Promise.all([first, second])
  })

  it('frees the slot when a dial rejects', async () => {
    const gate = new PooledRemoteDialGate({ delay: async () => {}, limit: 1 })

    await expect(
      gate.run('conn:remote', () => {
        throw new Error('ssh: connect to host remote port 22: Connection refused')
      })
    ).rejects.toThrow('Connection refused')

    await expect(gate.run('conn:remote', async () => 'ok')).resolves.toBe('ok')
    expect(gate.active('conn:remote')).toBe(0)
  })
})

describe('revalidatePooledRemoteBackends', () => {
  interface TestRemoteConnection {
    authMode?: string
    baseUrl: string
    process?: unknown
    remoteBaseUrl?: null | string
  }

  const harness = (
    rawEntries: Array<[string, { process?: unknown; remoteBaseUrl?: null | string; authMode?: string }]>
  ) => {
    const entries: Array<[string, TestRemoteConnection & { connectionPromise: Promise<TestRemoteConnection> }]> =
      rawEntries.map(([profile, entry]) => {
        const connection = { ...entry, baseUrl: String(entry.remoteBaseUrl || '') }

        return [profile, { ...connection, connectionPromise: Promise.resolve(connection) }]
      })

    const unreachable = new Set<string>()
    const log = vi.fn()
    const stopBackend = vi.fn()

    const probe = vi.fn(async (connection: TestRemoteConnection) => {
      if ([...unreachable].some(base => connection.remoteBaseUrl?.startsWith(base))) {
        throw new Error('unreachable')
      }

      return {}
    })

    return {
      log,
      probe,
      stopBackend,
      unreachable,
      run: (tracker: RemoteLivenessTracker) =>
        revalidatePooledRemoteBackends({ entries, log, probe, stopBackend, tracker })
    }
  }

  it('probes only pooled entries backed by a remote host', async () => {
    const local = { process: {}, remoteBaseUrl: null }
    const spawning = { process: null, remoteBaseUrl: null }
    const remote = { process: null, remoteBaseUrl: 'https://remote.example.com' }

    const pool = harness([
      ['local', local],
      ['spawning', spawning],
      ['remote', remote]
    ])

    await pool.run(new RemoteLivenessTracker())

    expect(pool.probe).toHaveBeenCalledTimes(1)
    expect(pool.probe).toHaveBeenCalledWith(
      expect.objectContaining({ remoteBaseUrl: 'https://remote.example.com' }),
      '/api/status',
      { timeoutMs: REMOTE_LIVENESS_TIMEOUT_MS }
    )
    expect(pool.stopBackend).not.toHaveBeenCalled()
  })

  it('passes the authenticated OAuth descriptor to the liveness probe', async () => {
    const remote = {
      process: null,
      remoteBaseUrl: 'https://remote.example.com',
      authMode: 'oauth'
    }

    const pool = harness([['oauth', remote]])

    await pool.run(new RemoteLivenessTracker())

    expect(pool.probe).toHaveBeenCalledWith(expect.objectContaining({ authMode: 'oauth' }), '/api/status', {
      timeoutMs: REMOTE_LIVENESS_TIMEOUT_MS
    })
    expect(pool.stopBackend).not.toHaveBeenCalled()
  })

  it('drops a descriptor only after the shared failure limit', async () => {
    const pool = harness([['coder', { process: null, remoteBaseUrl: 'https://remote.example.com/' }]])
    pool.unreachable.add('https://remote.example.com')

    const tracker = new RemoteLivenessTracker()

    for (let attempt = 1; attempt < REMOTE_LIVENESS_FAILURE_LIMIT; attempt += 1) {
      await expect(pool.run(tracker)).resolves.toEqual({ dropped: [] })
      expect(pool.stopBackend).not.toHaveBeenCalled()
    }

    await expect(pool.run(tracker)).resolves.toEqual({ dropped: ['coder'] })
    expect(pool.stopBackend).toHaveBeenCalledWith('coder')
  })

  it('accumulates failures across probes minutes apart under the pooled failure window', async () => {
    // Regression test for #94381: the pool revalidation tick is driven by the
    // renderer's reconnect IPC, which fires minutes apart once the primary
    // connection is healthy — far beyond the primary's 60s failure window.
    // With that window a dead pooled descriptor's streak reset on every tick
    // and the drop path was never reached; the pool served ECONNRESETs until
    // app restart.
    const pool = harness([['coder', { process: null, remoteBaseUrl: 'https://remote.example.com' }]])
    pool.unreachable.add('https://remote.example.com')

    let now = 1_000_000

    const tracker = new RemoteLivenessTracker(
      REMOTE_LIVENESS_FAILURE_LIMIT,
      REMOTE_POOLED_LIVENESS_FAILURE_WINDOW_MS,
      () => now
    )

    for (let attempt = 1; attempt < REMOTE_LIVENESS_FAILURE_LIMIT; attempt += 1) {
      await expect(pool.run(tracker)).resolves.toEqual({ dropped: [] })
      expect(pool.stopBackend).not.toHaveBeenCalled()
      now += 4 * 60_000 // observed production cadence: ~4 minutes between probes
    }

    await expect(pool.run(tracker)).resolves.toEqual({ dropped: ['coder'] })
    expect(pool.stopBackend).toHaveBeenCalledWith('coder')
  })

  it('still resets a genuinely stale streak past the pooled window', async () => {
    const pool = harness([['coder', { process: null, remoteBaseUrl: 'https://remote.example.com' }]])
    pool.unreachable.add('https://remote.example.com')

    let now = 1_000_000

    const tracker = new RemoteLivenessTracker(
      REMOTE_LIVENESS_FAILURE_LIMIT,
      REMOTE_POOLED_LIVENESS_FAILURE_WINDOW_MS,
      () => now
    )

    // Two failures 20 minutes apart (beyond the pooled window): each must
    // count as a fresh streak of 1, not silently accumulate across outages.
    await expect(pool.run(tracker)).resolves.toEqual({ dropped: [] })
    now += 20 * 60_000
    await expect(pool.run(tracker)).resolves.toEqual({ dropped: [] })
    now += 4 * 60_000
    await expect(pool.run(tracker)).resolves.toEqual({ dropped: [] })
    expect(pool.stopBackend).not.toHaveBeenCalled()
  })

  it('clears the streak when the host answers again', async () => {
    const pool = harness([['coder', { process: null, remoteBaseUrl: 'https://remote.example.com' }]])
    const tracker = new RemoteLivenessTracker()

    pool.unreachable.add('https://remote.example.com')
    await pool.run(tracker)

    pool.unreachable.clear()
    await pool.run(tracker)

    pool.unreachable.add('https://remote.example.com')

    for (let attempt = 1; attempt < REMOTE_LIVENESS_FAILURE_LIMIT; attempt += 1) {
      await expect(pool.run(tracker)).resolves.toEqual({ dropped: [] })
    }

    expect(pool.stopBackend).not.toHaveBeenCalled()
    await expect(pool.run(tracker)).resolves.toEqual({ dropped: ['coder'] })
  })

  it('keeps a healthy sibling when another profile on a different host dies', async () => {
    const pool = harness([
      ['coder', { process: null, remoteBaseUrl: 'https://dead.example.com' }],
      ['writer', { process: null, remoteBaseUrl: 'https://live.example.com' }]
    ])

    pool.unreachable.add('https://dead.example.com')

    const tracker = new RemoteLivenessTracker()

    for (let attempt = 1; attempt < REMOTE_LIVENESS_FAILURE_LIMIT; attempt += 1) {
      await pool.run(tracker)
    }

    await expect(pool.run(tracker)).resolves.toEqual({ dropped: ['coder'] })
    expect(pool.stopBackend).toHaveBeenCalledTimes(1)
    expect(pool.stopBackend).toHaveBeenCalledWith('coder')
  })
})
