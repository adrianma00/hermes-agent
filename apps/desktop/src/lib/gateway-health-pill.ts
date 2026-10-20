/**
 * Status-bar "backend health" pill. The serve/chat socket and the messaging
 * gateway are different processes; this derivation is the one place that
 * names them so the painted label cannot be read as "Discord is up".
 */

import { runtimeReadinessDisplay, type RuntimeReadinessResult } from '@/lib/runtime-readiness'

export interface GatewayHealthPillCopy {
  backend: string
  checking: string
  connecting: string
  messagingDegraded: (name: string) => string
  messagingStopped: string
  needsSetup: string
  offline: string
  ready: string
  restarting: string
  unavailable: string
  /** Detail when the connected backend is running stale code (`code_skew`). */
  staleCode: string
}

export interface GatewayHealthPlatform {
  state: string
}

export interface GatewayHealthPillInput {
  /** Backend health as the server reports it (`code_skew` present). `null` = not probed yet. */
  codeSkewed?: boolean | null
  connectionState: string
  copy: GatewayHealthPillCopy
  inferenceStatus: RuntimeReadinessResult | null
  messagingRunning?: boolean
  messagingState?: null | string
  platforms?: null | Record<string, GatewayHealthPlatform>
  restarting?: boolean
}

export interface GatewayHealthPill {
  degraded: boolean
  detail: string
  label: string
  title?: string
}

const HEALTHY_PLATFORM_STATES = new Set(['connected'])
const IGNORED_PLATFORM_STATES = new Set(['disabled', 'not_configured', 'not configured'])

function platformLeaf(id: string): string {
  const sep = id.lastIndexOf(':')

  return sep >= 0 ? id.slice(sep + 1) : id
}

function unhealthyPlatformNames(platforms: GatewayHealthPillInput['platforms']): string[] {
  return Object.entries(platforms || {})
    .filter(([, platform]) => {
      const state = (platform?.state || '').trim().toLowerCase()

      return Boolean(state) && !HEALTHY_PLATFORM_STATES.has(state) && !IGNORED_PLATFORM_STATES.has(state)
    })
    .map(([id]) => platformLeaf(id))
}

function messagingWasConfigured(
  messagingState: GatewayHealthPillInput['messagingState'],
  platforms: GatewayHealthPillInput['platforms']
): boolean {
  const state = (messagingState || '').trim().toLowerCase()

  if (state === 'stopped' || state === 'startup_failed') {
    return true
  }

  return Object.keys(platforms || {}).length > 0
}

function messagingDetail(copy: GatewayHealthPillCopy, names: string[]): string {
  return names.length > 0 ? copy.messagingDegraded(names[0]!) : copy.messagingStopped
}

function inferenceDetail(copy: GatewayHealthPillCopy, inferenceStatus: RuntimeReadinessResult | null): string {
  return {
    checking: copy.checking,
    needs_setup: copy.needsSetup,
    ready: copy.ready,
    unavailable: copy.unavailable
  }[runtimeReadinessDisplay(inferenceStatus)]
}

export function statusBarGatewayHealth({
  codeSkewed = null,
  connectionState,
  copy,
  inferenceStatus,
  messagingRunning,
  messagingState,
  platforms,
  restarting = false
}: GatewayHealthPillInput): GatewayHealthPill {
  const connectionOpen = connectionState === 'open'
  const connectionConnecting = connectionState === 'connecting'
  const downNames = unhealthyPlatformNames(platforms)

  const messagingDown =
    messagingWasConfigured(messagingState, platforms) && (messagingRunning === false || downNames.length > 0)

  const connectionDetail = connectionOpen
    ? inferenceDetail(copy, inferenceStatus)
    : connectionConnecting
      ? copy.connecting
      : copy.offline

  // Stale code outranks the inference verdict: the process is serving modules
  // older than the checkout, so "ready" is the one thing this pill must not say.
  // Only while connected — a down backend has no verdict to colour.
  const stale = connectionOpen && codeSkewed === true

  const detail = restarting
    ? copy.restarting
    : stale
      ? copy.staleCode
      : messagingDown && connectionOpen
        ? messagingDetail(copy, downNames)
        : connectionDetail

  return {
    degraded: Boolean((messagingDown && connectionOpen) || stale),
    detail,
    label: copy.backend,
    title: stale ? copy.staleCode : messagingDown && connectionOpen ? messagingDetail(copy, downNames) : undefined
  }
}
