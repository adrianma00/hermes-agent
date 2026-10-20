import { describe, expect, it } from 'vitest'

import { isCodeSkewRestartRequired, isModelSwitchSkewConfirm } from './code-skew-error'

const SKEW_IPC =
  'Error invoking remote method \'hermes:api\': Error: 503: {"detail":"Restart required: This process is running code from 08b4875f4a but the checkout on disk is now 48d2528066. The model picker would risk a stale-module crash — restart the Desktop-owned backend to load the new code (use Restart backend in Hermes Desktop, or quit and reopen the app)"}'

// The exact text `config.set model` returns as `confirm_message` when the
// gateway guard refuses a skewed switch (locales/en.yaml → gateway.model.err_skew).
const SKEW_CONFIRM =
  'Error: This gateway is running code from 113624af7a but the checkout on disk is now c46ebe1b9a. Switching models would risk a stale-module crash — restart the gateway to load the new code: hermes gateway restart'

describe('isCodeSkewRestartRequired', () => {
  it('detects the Models-page IPC 503 from a stale Desktop-owned backend', () => {
    expect(isCodeSkewRestartRequired(new Error(SKEW_IPC))).toBe(true)
  })

  it('detects a bare FastAPI detail string', () => {
    expect(isCodeSkewRestartRequired('Restart required: checkout drifted')).toBe(true)
  })

  it('ignores unrelated 503s and load failures', () => {
    expect(isCodeSkewRestartRequired(new Error('503: Service Unavailable'))).toBe(false)
    expect(isCodeSkewRestartRequired(new Error('Failed to load models'))).toBe(false)
    expect(isCodeSkewRestartRequired(null)).toBe(false)
  })
})

describe('isModelSwitchSkewConfirm', () => {
  // This discriminator decides whether the switch dialog offers
  // "Restart backend / Switch anyway" or the ordinary apply/cancel pair, so a
  // false positive would replace every selection guard's dialog.
  it('detects the gateway skew confirm message', () => {
    expect(isModelSwitchSkewConfirm(new Error(SKEW_CONFIRM))).toBe(true)
    expect(isModelSwitchSkewConfirm(SKEW_CONFIRM)).toBe(true)
  })

  it('does NOT claim an ordinary selection guard', () => {
    // The expensive/data-training guard: apply/cancel is the right pair here.
    expect(isModelSwitchSkewConfirm('This contributor model trains on your data.')).toBe(false)
    expect(isModelSwitchSkewConfirm('Muse Spark costs $75/M output tokens.')).toBe(false)
    expect(isModelSwitchSkewConfirm(null)).toBe(false)
    expect(isModelSwitchSkewConfirm(undefined)).toBe(false)
  })
})
