import { renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ clearCodeSkew: vi.fn() }))

// Only the verdict store is mocked. `@/store/profile` and the project-tree hook
// pull in a large transitive graph (layout, projects, session) whose atoms are
// read at import time by other modules, so partially mocking them fails the
// module under test for reasons unrelated to the behaviour here.
vi.mock('@/store/code-skew', () => ({ clearCodeSkew: mocks.clearCodeSkew }))

import { useGatewayScopeRefresh } from './wiring-effects'

// A skew verdict describes ONE backend process: `code_skew` is computed from the
// revision the gateway booted with versus the checkout on disk. It therefore
// belongs to the (connection, profile) scope that answered, and must not outlive
// it.
//
// It did, and the failure was silent and confusing: the pill and the model menu
// kept painting "stale code" after the gateway had been restarted and was clean.
// The probe cannot correct it either — it reads whichever connection the app is
// attached to NOW, which may be a different machine entirely — so the old verdict
// had no way to be retired. A scope change is the reliable place to drop it.
//
// The sibling resets below are the established pattern for scope-owned state;
// these pin the skew verdict to the same rule.
describe('the gateway scope drops the previous backend\u2019s skew verdict', () => {
  const refreshCurrentModel = vi.fn(async () => {})
  const refreshHermesConfig = vi.fn(async () => {})

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('clears when the scope changes', () => {
    const { rerender } = renderHook(
      ({ scope }: { scope: string }) =>
        useGatewayScopeRefresh(scope, 'default', refreshCurrentModel, refreshHermesConfig),
      { initialProps: { scope: 'local' } }
    )

    rerender({ scope: 'surface' })

    expect(mocks.clearCodeSkew).toHaveBeenCalled()
  })

  it('does not clear on the mounting render', () => {
    // The mount pass ESTABLISHES the baseline scope. Clearing there would wipe a
    // verdict the app had just fetched, on every render.
    renderHook(() =>
      useGatewayScopeRefresh('local', 'default', refreshCurrentModel, refreshHermesConfig)
    )

    expect(mocks.clearCodeSkew).not.toHaveBeenCalled()
  })

  it('does not clear while the scope is unchanged', () => {
    const { rerender } = renderHook(
      ({ scope }: { scope: string }) =>
        useGatewayScopeRefresh(scope, 'default', refreshCurrentModel, refreshHermesConfig),
      { initialProps: { scope: 'local' } }
    )

    rerender({ scope: 'local' })
    rerender({ scope: 'local' })

    expect(mocks.clearCodeSkew).not.toHaveBeenCalled()
  })

  it('clears in the same pass as the other scope-owned resets', () => {
    const { rerender } = renderHook(
      ({ scope }: { scope: string }) =>
        useGatewayScopeRefresh(scope, 'default', refreshCurrentModel, refreshHermesConfig),
      { initialProps: { scope: 'local' } }
    )

    rerender({ scope: 'surface' })

    expect(mocks.clearCodeSkew).toHaveBeenCalledTimes(1)
    expect(refreshCurrentModel).toHaveBeenCalledWith(true)
    expect(refreshHermesConfig).toHaveBeenCalledWith(true)
  })
})
