import { expect, test } from 'vitest'

import { recycleOwnedBackend } from './backend-recycle'
import { type GatewayEndpoint, gatewayOwnerProfile, localGatewayRestartTarget } from './local-gateway'

// "Restart backend" (Models page, and the connection-lost toast) restarts the local gateway behind a
// cached descriptor. A dropped connection invalidates that descriptor — the failure the user is
// recovering from — so the endpoint that names the owner is gone exactly when the restart is
// offered. The button then found no endpoint and silently did nothing.

const noneRemote = () => false

test('a missing descriptor falls back to the profile name, not a silent no-op', () => {
  expect(localGatewayRestartTarget('', 'default', noneRemote)).toBe('default')
  expect(localGatewayRestartTarget('paid-ads', 'default', noneRemote)).toBe('paid-ads')
})

test('an empty or matching profile targets the primary', () => {
  // The renderer sends `primaryConnection?.profile`, which is '' once the connection is gone: the
  // descriptor is missing AND the key is empty, so the fallback must resolve the primary itself
  // rather than hand an empty profile to the CLI.
  expect(localGatewayRestartTarget('', 'work', noneRemote)).toBe('work')
  expect(localGatewayRestartTarget('work', 'work', noneRemote)).toBe('work')
  expect(localGatewayRestartTarget('   ', 'work', noneRemote)).toBe('work')
})

test('a remote profile is never restarted locally', () => {
  // A remote/SSH backend has no local gateway: restarting a local one for it would start a gateway
  // the profile does not use, on the port its own remote already serves.
  expect(localGatewayRestartTarget('ssh-box', 'default', p => p === 'ssh-box')).toBeNull()
  expect(localGatewayRestartTarget('', 'default', () => true)).toBeNull()
})

test('an endpoint still names the owner itself, which the fallback must agree with', () => {
  // The descriptor path stays authoritative where it exists: a served secondary's endpoint resolves
  // to the multiplexer root, and the fallback resolves that same profile to the same served home.
  expect(gatewayOwnerProfile({ profile_id: '/h/profiles/ops', control_home: '/h' } as never)).toBe('default')
  expect(localGatewayRestartTarget('ops', 'default', noneRemote)).toBe('ops')
})

test('the recycle restarts the local gateway even when a drop already invalidated the descriptor', async () => {
  // The reported path end to end, through the real recycle: by the time the user clicks, the drain
  // path has already cleared the descriptor, so the owner cannot be read from an endpoint. The
  // restart must still happen — pre-fix the primary branch found no endpoint and did nothing.
  const descriptor: GatewayEndpoint | null = null
  const restarted: string[] = []

  const target = await recycleOwnedBackend({
    notifyApplied: () => undefined,
    primaryProfile: 'default',
    profile: '',
    // The real `restartAttachedLocalGateway` shape: prefer the descriptor's owner, else fall back.
    restartLocalGateway: async profile => {
      const owner = descriptor ? gatewayOwnerProfile(descriptor) : localGatewayRestartTarget(profile, 'default', noneRemote)

      if (owner) {
        restarted.push(owner)
      }
    },
    teardownPool: async () => undefined,
    teardownPrimary: async () => undefined,
    teardownSsh: async () => undefined
  })

  expect(target).toBe('primary')
  // Pre-fix, `restarted` stayed empty: the no-op the report describes.
  expect(restarted).toEqual(['default'])
})
