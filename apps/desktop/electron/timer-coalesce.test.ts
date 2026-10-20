import { describe, expect, it } from 'vitest'

import { createCoalescingInterval } from './timer-coalesce'

describe('createCoalescingInterval', () => {
  it('fires the callback normally when there is no lag', () => {
    const calls: number[] = []

    const { clear } = createCoalescingInterval(
      () => { calls.push(1) },
      1_000,
      { getLastLagMs: () => 0, thresholdMs: 2_000 }
    )

    expect(typeof clear).toBe('function')
    clear()
  })

  it('returns a clear function that can be called', () => {
    const { clear } = createCoalescingInterval(
      () => { /* noop */ },
      1_000,
      { getLastLagMs: () => 0, thresholdMs: 2_000 }
    )

    expect(typeof clear).toBe('function')
    expect(() => clear()).not.toThrow()
  })

  it('works with the default zero-lag signal', () => {
    let callCount = 0

    const { clear } = createCoalescingInterval(
      () => { callCount++ },
      500,
      { getLastLagMs: () => 0, thresholdMs: 2_000 }
    )

    clear()
    expect(callCount).toBe(0)
  })
})

describe('createCoalescingInterval freeze/thaw coalescing (structural)', () => {
  it('fires once after a lag event, skipping backlogged ticks', () => {
    const calls: number[] = []
    let lagMs = 0
    let capturedCallback: (() => void) | undefined

    const originalSetInterval = globalThis.setInterval
    const originalClearInterval = globalThis.clearInterval

    globalThis.setInterval = ((cb: () => void, _ms: number) => {
      capturedCallback = cb

      return 42 as unknown as ReturnType<typeof setInterval>
    }) as typeof globalThis.setInterval

    globalThis.clearInterval = (_timer: ReturnType<typeof setInterval>) => {
      // noop
    }

    try {
      const { clear } = createCoalescingInterval(
        () => { calls.push(calls.length + 1) },
        30_000,
        { getLastLagMs: () => lagMs, thresholdMs: 2_000 }
      )

      // Simulate a freeze: set the lag signal high
      lagMs = 42 * 60 * 1000 // 42 minutes

      // Fire the callback — this simulates the event loop thaw
      capturedCallback?.()

      // Only ONE callback should have fired despite the huge lag
      expect(calls.length).toBe(1)

      // Now simulate a normal tick with no lag
      lagMs = 0
      capturedCallback?.()

      // A second normal callback fires
      expect(calls.length).toBe(2)

      clear()
    } finally {
      globalThis.setInterval = originalSetInterval
      globalThis.clearInterval = originalClearInterval
    }
  })

  it('fires every normal tick when there is no lag', () => {
    const calls: number[] = []
    let lagMs = 0
    let capturedCallback: (() => void) | undefined

    const originalSetInterval = globalThis.setInterval
    const originalClearInterval = globalThis.clearInterval

    globalThis.setInterval = ((cb: () => void, _ms: number) => {
      capturedCallback = cb

      return 42 as unknown as ReturnType<typeof setInterval>
    }) as typeof globalThis.setInterval

    globalThis.clearInterval = (_timer: ReturnType<typeof setInterval>) => {
      // noop
    }

    try {
      const { clear } = createCoalescingInterval(
        () => { calls.push(calls.length + 1) },
        1_000,
        { getLastLagMs: () => lagMs, thresholdMs: 2_000 }
      )

      // Three normal ticks with no lag
      capturedCallback?.()
      capturedCallback?.()
      capturedCallback?.()

      expect(calls).toEqual([1, 2, 3])

      clear()
    } finally {
      globalThis.setInterval = originalSetInterval
      globalThis.clearInterval = originalClearInterval
    }
  })
})
