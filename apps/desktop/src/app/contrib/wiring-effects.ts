import { useStore } from '@nanostores/react'
import { useEffect, useRef } from 'react'
import type { NavigateFunction } from 'react-router'

import { clearCodeSkew } from '@/store/code-skew'
import { $freshSessionRequest, refreshActiveProfile } from '@/store/profile'

import { resetProjectTreeState } from '../right-sidebar/files/use-project-tree'
import { SETTINGS_ROUTE } from '../routes'

import { $restartPreviewServer } from './panes'

type RestartPreviewServer = NonNullable<ReturnType<typeof $restartPreviewServer.get>>

// Palette "Keyboard shortcuts" entry dispatches a custom event (contributions
// don't have router access); listen and navigate to the settings keybinds tab.
export function useOpenKeybindsListener(navigate: NavigateFunction) {
  useEffect(() => {
    const onOpenKeybinds = () => navigate(`${SETTINGS_ROUTE}?tab=keybinds`)
    window.addEventListener('hermes:open-keybinds', onOpenKeybinds)

    return () => window.removeEventListener('hermes:open-keybinds', onOpenKeybinds)
  }, [navigate])
}

// Dev-only: install the credit-notice demo trigger (Ctrl+Shift+C / ⌘K palette
// / window.__creditsDemo). Dynamic import inside the DEV guard so the module
// is dropped from production builds.
export function useCreditsNoticeDemo() {
  useEffect(() => {
    if (!import.meta.env.DEV) {
      return
    }

    let dispose: (() => void) | undefined

    void import('./dev/credits-notice-demo').then(m => {
      dispose = m.installCreditsNoticeDemo()
    })

    return () => dispose?.()
  }, [])
}

// Expose the restart handler to the preview pane contribution (module
// boundary crossed via atom — contrib-panes can't import wiring.tsx).
export function usePublishRestartPreviewServer(restartPreviewServer: RestartPreviewServer) {
  useEffect(() => {
    $restartPreviewServer.set(restartPreviewServer)

    return () => $restartPreviewServer.set(null)
  }, [restartPreviewServer])
}

// A profile switch/create drops to a fresh new-session draft so the
// previously open session doesn't bleed across contexts. Skip initial value.
export function useFreshSessionRequest(startFreshSessionDraft: () => void) {
  const freshSessionRequest = useStore($freshSessionRequest)
  const lastFreshRef = useRef(freshSessionRequest)

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    if (freshSessionRequest === lastFreshRef.current) {
      return
    }

    lastFreshRef.current = freshSessionRequest
    startFreshSessionDraft()
  }, [freshSessionRequest, startFreshSessionDraft])
}

// Swapping the live gateway to another source or profile must re-pull that
// source's model/config/profile state. Two sources commonly both expose a
// `default` profile, so profile alone is not a sufficient identity.
export function useGatewayScopeRefresh(
  activeConnectionId: null | string,
  activeGatewayProfile: string,
  refreshCurrentModel: (force: boolean) => Promise<unknown>,
  refreshHermesConfig: (force: boolean) => Promise<unknown>
) {
  const gatewayScope = `${activeConnectionId ?? ''}\0${activeGatewayProfile}`
  const lastGatewayScopeRef = useRef(gatewayScope)

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    if (gatewayScope === lastGatewayScopeRef.current) {
      return
    }

    lastGatewayScopeRef.current = gatewayScope
    // Force: the new source/profile pair has its own defaults, so reseed the
    // selector even if the composer already shows values from the previous
    // backend. These refreshes carry intent tokens so an in-flight picker
    // click still wins.
    void refreshCurrentModel(true)
    void refreshHermesConfig(true)
    void refreshActiveProfile()
    resetProjectTreeState()
    // A skew verdict describes ONE backend's process, so it cannot outlive the
    // scope it came from. Without this, switching from a skewed gateway to
    // another connection left the pill and the model menu painting "stale code"
    // for a backend no longer in view — and the probe cannot correct it, because
    // it reads the connection you are NOW attached to, which has a different
    // (and possibly clean) revision. `null` = not asked yet, which renders the
    // ordinary state until this scope's own probe answers.
    clearCodeSkew()
  }, [gatewayScope, refreshCurrentModel, refreshHermesConfig])
}
