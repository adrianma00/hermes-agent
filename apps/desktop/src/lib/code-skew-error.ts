/** True when a backend error is the post-update stale-module 503 (#97046). */
export function isCodeSkewRestartRequired(error: unknown): boolean {
  return /Restart required:/i.test(errorText(error))
}

/**
 * True when a model-switch `confirm_message` is the "stale code" guard rather
 * than a selection guard (cost, data policy).
 *
 * The two need different dialogs: a selection guard asks apply/cancel, while
 * skew's real choices are RESTART the backend or switch anyway. Match the
 * message rather than an error code because `config.set model` answers this as
 * a `confirm_required` envelope (the same shape as every other guard), so the
 * text is the only thing distinguishing them on the wire.
 */
export function isModelSwitchSkewConfirm(message: unknown): boolean {
  const text = errorText(message)

  // "…would risk a stale-module crash — restart the gateway to load the new
  // code" — the stale-module phrase is unique to this guard; the revision
  // pairing is the belt to that brace.
  return /stale-module crash/i.test(text) || /checkout on disk is now/i.test(text)
}

function errorText(error: unknown): string {
  if (error instanceof Error) {
    return error.message
  }

  return typeof error === 'string' ? error : String(error ?? '')
}
