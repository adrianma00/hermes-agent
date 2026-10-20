import { describe, expect, it } from 'vitest'

import { en } from '@/i18n/en'

import { statusBarGatewayHealth } from './gateway-health-pill'

const copy = {
  backend: en.shell.statusbar.backend,
  checking: en.shell.statusbar.gatewayChecking,
  connecting: en.shell.statusbar.gatewayConnecting,
  messagingDegraded: en.shell.statusbar.messagingDegraded,
  messagingStopped: en.shell.statusbar.messagingStopped,
  needsSetup: en.shell.statusbar.gatewayNeedsSetup,
  offline: en.shell.statusbar.gatewayOffline,
  ready: en.shell.statusbar.gatewayReady,
  restarting: en.shell.statusbar.gatewayRestarting,
  staleCode: en.shell.statusbar.staleCode,
  unavailable: en.shell.statusbar.gatewayUnavailable
}

const inferenceReady = {
  checksDisagree: false,
  ready: true,
  reason: null,
  source: 'runtime_check' as const
}

const openReady = {
  connectionState: 'open',
  copy,
  inferenceStatus: inferenceReady
}

describe('statusBarGatewayHealth', () => {
  it('does not paint Gateway ready when the serve socket is up and messaging is down', () => {
    const pill = statusBarGatewayHealth({
      ...openReady,
      messagingRunning: false,
      messagingState: 'stopped',
      platforms: {}
    })

    expect(`${pill.label} ${pill.detail}`).not.toBe('Gateway ready')
    expect(pill.label).toBe(copy.backend)
    expect(pill.detail).toBe(copy.messagingStopped)
    expect(pill.degraded).toBe(true)
  })

  it('stays backend-ready when messaging was never configured, and names a down platform while the process is up', () => {
    const quiet = statusBarGatewayHealth({
      ...openReady,
      messagingRunning: false,
      messagingState: null,
      platforms: {}
    })

    expect(`${quiet.label} ${quiet.detail}`).toBe(`${copy.backend} ${copy.ready}`)
    expect(quiet.degraded).toBe(false)

    const discordDown = statusBarGatewayHealth({
      ...openReady,
      messagingRunning: true,
      messagingState: 'running',
      platforms: { discord: { state: 'fatal' } }
    })

    expect(discordDown.detail).toBe(copy.messagingDegraded('discord'))
    expect(discordDown.degraded).toBe(true)
  })

  // The backend pill is the ONE surface always on screen, so it must not read
  // "ready" for a process serving code older than the checkout. Reported as
  // `code_skew` on the server's health endpoint.
  it('names stale code instead of "ready" while the backend is skewed', () => {
    const pill = statusBarGatewayHealth({ ...openReady, codeSkewed: true })

    expect(pill.detail).toBe(copy.staleCode)
    expect(pill.detail).not.toBe(copy.ready)
    expect(pill.degraded).toBe(true)
    expect(pill.title).toBe(copy.staleCode)
  })

  it('outranks the inference verdict while skewed', () => {
    // A skewed process answers readiness fine; that is exactly the trap — the
    // pill would otherwise paint a green "ready" over stale modules.
    const pill = statusBarGatewayHealth({ ...openReady, codeSkewed: true, inferenceStatus: inferenceReady })

    expect(pill.detail).toBe(copy.staleCode)
  })

  it('keeps the ordinary verdict when not skewed, and when unprobed', () => {
    expect(statusBarGatewayHealth({ ...openReady, codeSkewed: false }).detail).toBe(copy.ready)
    // `null` = never probed: an unasked question must not paint a fault.
    expect(statusBarGatewayHealth({ ...openReady, codeSkewed: null }).detail).toBe(copy.ready)
    expect(statusBarGatewayHealth({ ...openReady, codeSkewed: null }).degraded).toBe(false)
  })

  it('does not colour a disconnected backend with the skew verdict', () => {
    // A down socket has no live process to describe; "connecting" is the truth.
    const pill = statusBarGatewayHealth({
      ...openReady,
      codeSkewed: true,
      connectionState: 'connecting'
    })

    expect(pill.detail).toBe(copy.connecting)
    expect(pill.degraded).toBe(false)
  })
})
