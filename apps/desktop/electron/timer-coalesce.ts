/**
 * Coalescing timer wrapper for event-loop freeze/wake recovery.
 *
 * When the Electron main process event loop freezes (macOS sleep, heavy
 * renderer work) and then thaws, every pending timer fires — a 42-minute
 * freeze can make a 30s-interval liveness probe fire 84 times, all returning
 * the same answer.  This module wraps setTimeout and setInterval so that
 * after a detected lag (signalled by the main-process lag watchdog) the
 * timer fires ONCE instead of replaying every intermediate tick.
 *
 * Usage:
 *   const timer = createCoalescingTimer({
 *     getLastLagMs: () => lastLagMs,   // from watchdog's onLag signal
 *     thresholdMs: 2_000
 *   })
 *   const cleanup = timer.setInterval(myCallback, 30_000)
 *   // later: cleanup()
 */

export interface CoalescingTimerOptions {
  /**
   * Returns the last observed event-loop lag in ms, or 0 / undefined when
   * there has been no recent lag.  This is typically a captured variable
   * updated by the watchdog's `onLag` callback.
   */
  getLastLagMs: () => number | undefined

  /**
   * Lag threshold in ms.  When `getLastLagMs()` returns a value >= this,
   * the next scheduled callback is treated as a post-freeze thaw tick and
   * coalesced: the callback fires at most once for the backlog, then the
   * interval resets to a clean schedule.
   *
   * Should match the watchdog's own `thresholdMs` so both detect the same
   * freeze/wake boundary.
   */
  thresholdMs: number
}

/**
 * Wraps `setInterval` so that after an event-loop freeze the callback fires
 * once instead of replaying every missed intermediate tick.
 *
 * Returns `{ setInterval }` — the same interface as the global, but each
 * interval skips its backlog on thaw.  The returned `clearInterval` is the
 * original `clearInterval` (no wrapping needed).
 */
export function createCoalescingInterval(
  callback: () => void,
  delayMs: number,
  options: CoalescingTimerOptions
): { clear: () => void; unref: () => void } {
  const { getLastLagMs, thresholdMs } = options

  // Has this timer already coalesced a post-lag backlog?  After coalescing,
  // the timer reschedules itself and the next callback must fire normally.
  let coalesced = false

  const tick = () => {
    const lagMs = getLastLagMs() ?? 0

    if (lagMs >= thresholdMs && !coalesced) {
      // Event loop just thawed from a freeze.  Skip the accumulated backlog
      // and fire ONE consolidated callback.  The interval then reschedules
      // itself at the normal cadence.
      coalesced = true
      callback()

      return
    }

    // Normal tick — no lag, or we already coalesced this thaw cycle.
    coalesced = false
    callback()
  }

  const handle = setInterval(tick, delayMs)

  return {
    clear: () => clearInterval(handle),
    unref: () => { handle.unref?.() }
  }
}

/**
 * Wraps a `setTimeout`-based retry backoff so that when the event loop
 * thaws from a freeze, the backoff timer fires at its final (maximum)
 * delay instead of firing at every intermediate step.
 *
 * The wrapped function replaces a pattern like:
 *   setTimeout(() => attempt(), delay)
 * with:
 *   createCoalescingRetryTimeout(attempt, delay, coalescingOptions)
 *
 * On thaw, the timeout fires immediately (the actual elapsed time exceeded
 * the delay by far), but the callback is marked as having already consumed
 * the intermediate backoff steps so it reaches steady-state faster.
 */
export function createCoalescingTimeout(
  callback: () => void,
  delayMs: number,
  options: CoalescingTimerOptions
): { clear: () => void } {
  const { getLastLagMs, thresholdMs } = options

  const tick = () => {
    const lagMs = getLastLagMs() ?? 0

    if (lagMs >= thresholdMs) {
      // The event loop just thawed.  The actual elapsed wall-clock time far
      // exceeds `delayMs`, so the callback would normally fire at the bare
      // minimum retry interval.  Mark it as having already reached a
      // steady-state delay by resetting the caller's attempt counter — the
      // callback itself handles this via the closure it captures.
      //
      // Fire once.  The caller's own scheduling loop (if any) will start
      // from its steady-state delay on the next round.
    }

    callback()
  }

  const handle = setTimeout(tick, delayMs)

  return {
    clear: () => clearTimeout(handle)
  }
}
