import { translateNow } from '@/i18n'
import { confirm } from '@/store/confirm'
import { notify, notifyError } from '@/store/notifications'

/** The gateway's model-switch handshake shape — shared by `config.set model`
 *  and `profiles.configure` (Bots editor). `confirm_required: true` means the
 *  switch was intentionally NOT applied and the gateway is waiting for a
 *  resend that carries `confirm_expensive_model: true`. */
export interface GuardedModelSwitchResult {
  confirm_message?: string
  confirm_required?: boolean
  deferred?: boolean
}

export interface SurfaceModelSwitchConfirmOptions<T extends GuardedModelSwitchResult> {
  /** The gateway's `confirm_message`, when present. */
  confirmMessage?: string
  /** Error-toast copy when the confirmed resend still fails. */
  failureMessage: string
  /** Runs after the confirmed resend succeeds (cache invalidation etc). */
  finish?: (result: T | undefined) => void
  /** Staleness guard — the session/model can move on while the dialog is
   *  open. Return true to make the answered confirm a no-op (with a notice
   *  saying so) instead of clobbering the newer choice. */
  isStale?: () => boolean
  /** The model the switch targets — named in the dialog title when known. */
  model?: string
  /** Optimistically repaint the pending selection before the resend. */
  repaint?: () => void
  /** Resend the switch WITH `confirm_expensive_model: true`. */
  requestConfirmed: () => Promise<T | undefined>
  /** Undo the optimistic repaint when the confirmed resend fails. */
  rollback?: () => void
  /**
   * Opt-in "the backend is serving stale code" mode. The two real choices there
   * are not apply/cancel but RESTART the backend or switch anyway, so both
   * buttons are affirmatively labelled and the dismiss path means "go ahead".
   * Omitted for every ordinary guard, which keeps the stock apply/cancel pair.
   */
  skew?: {
    /** Label for the primary button: restart the backend instead of switching. */
    restartLabel: string
    /** Label for the secondary button: apply the switch despite the skew. */
    continueLabel: string
    /** Runs when the user takes the restart. The pick is NOT applied — the
     *  restart reloads the code and the user re-picks against fresh modules. */
    onRestart: () => Promise<void> | void
  }
}

/**
 * THE confirm flow for guarded model switches — every surface that can
 * receive `confirm_required` from a model switch (core picker via
 * `config.set`, Bots editor via `profiles.configure`, future surfaces) routes
 * it through here so there is exactly one applier and no forked confirm
 * logic per surface (#95293).
 *
 * A refused switch is a decision, not a notification: it asks through
 * `confirm()` — the app's ConfirmDialog — so there is a real decline button,
 * Esc/backdrop/✕ all mean "keep current model", and the dialog owns focus
 * while it waits (#112458). Declining is free: nothing was applied. Only a
 * confirmed answer resends with `confirm_expensive_model: true`; a resend
 * that STILL answers `confirm_required` is treated as a failure — the gateway
 * asked twice, something is wrong; never loop.
 *
 * Resolves `true` when the switch was applied, `false` when it was declined,
 * went stale, or failed (a failure surfaces its own toast).
 */
export async function surfaceModelSwitchConfirm<T extends GuardedModelSwitchResult>(
  options: SurfaceModelSwitchConfirmOptions<T>
): Promise<boolean> {
  const skew = options.skew

  // In skew mode the dialog is a two-way choice rather than apply/cancel: the
  // affirmative button takes the RESTART, and the dismiss path is a deliberate
  // "switch anyway" (never a silent abort — the pick is what the user asked
  // for). Labels therefore come from the caller, which knows the product copy.
  const accepted = await confirm({
    cancelLabel: skew ? skew.continueLabel : translateNow('desktop.modelSwitchKeepLabel'),
    confirmLabel: skew ? skew.restartLabel : translateNow('desktop.modelSwitchConfirmLabel'),
    description: options.confirmMessage?.trim() || translateNow('desktop.modelSwitchConfirmBody'),
    destructive: true,
    title: options.model
      ? translateNow('desktop.modelSwitchConfirmTitle', options.model)
      : translateNow('desktop.modelSwitchConfirmTitleFallback')
  })

  if (skew && accepted) {
    // The restart reloads the code; the switch itself is deliberately NOT
    // applied. Re-picking against fresh modules is the point — applying now
    // would resolve the model string against exactly the stale modules the
    // restart exists to replace.
    await skew.onRestart()

    return false
  }

  if (!skew && !accepted) {
    return false
  }

  if (options.isStale?.()) {
    // The session or model moved on under the dialog, so the switch it asked
    // for no longer exists. Say so — a silent no-op reads as a broken button.
    notify({ kind: 'info', message: translateNow('desktop.modelSwitchStaleNotice') })

    return false
  }

  options.repaint?.()

  try {
    const result = await options.requestConfirmed()

    if (result?.confirm_required) {
      throw new Error(result.confirm_message?.trim() || options.failureMessage)
    }

    options.finish?.(result)

    return true
  } catch (err) {
    options.rollback?.()
    notifyError(err, options.failureMessage)

    return false
  }
}
