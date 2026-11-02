import { describe, expect, it } from 'vitest'
import {
  apiKeyScopes, cardKey, cardLabel, instanceRoutes, ownerLabel, ownerTags,
  targetDisplay, taskFamily, taskLogRoute, taskRoute,
} from './contracts'

describe('identity and route contracts', () => {
  it('keys a card by service id only', () => {
    expect(cardKey('abc')).toBe('svc:abc')
  })
  it('builds ID routes and never a name path', () => {
    expect(instanceRoutes('s1').stop).toBe('/api/services/instances/s1/stop')
    expect(instanceRoutes('s1').remove()).toBe('/api/services/instances/s1?scope=service')
    expect(taskRoute('gt-1')).toBe('/tasks/gt-1')
    expect(taskLogRoute('api-2')).toBe('/api/tasks/api-2/log')
  })
  it('dispatches a task family by prefix', () => {
    expect(taskFamily('gt-abc')).toBe('gateway')
    expect(taskFamily('api-abc')).toBe('api')
  })
  it('renders the canonical target without an empty paren pair', () => {
    expect(targetDisplay('svc', '')).toBe('svc')
    expect(targetDisplay('svc', '.')).toBe('svc')
    expect(targetDisplay('svc', 'docker')).toBe('svc (docker)')
  })
})

describe('owner and key contracts', () => {
  it('derives independent owner tags', () => {
    expect(ownerTags({ role: 'special', is_public_shared: true }))
      .toEqual(['Functional', 'Public'])
    expect(ownerTags({ role: 'viewer', has_gateway_user: false })).toEqual(['Service-only'])
    expect(ownerTags({ role: 'special', deleted_at: 'x' })).toEqual(['Functional', 'owner gone'])
    expect(ownerTags({ role: 'special', was_public: true, deleted_at: 'x' }))
      .toEqual(['Functional', 'Public', 'owner gone'])
  })
  it('scopes the API keys page by role', () => {
    expect(apiKeyScopes('admin')).toEqual(['My API Keys', 'API Keys'])
    expect(apiKeyScopes('viewer')).toEqual(['My API Keys'])
    expect(apiKeyScopes('special')).toEqual([])
    expect(ownerLabel('alice', 'viewer')).toBe('alice:viewer')
  })
})

describe('one-card merge', () => {
  it('covers every §3.6 precedence row', () => {
    const failed = { service_id: 's', status: 'failed' }
    for (const s of ['Generating', 'Paused (input)', 'Refused (capacity)', 'Ready to deploy', 'Deploying…']) {
      expect(cardLabel(failed, s, false)).toBe(`Failed + ${s}`)
    }
    expect(cardLabel(failed, null, false)).toBe('Failed')
    expect(cardLabel({ service_id: 's', status: 'building' }, 'Paused (input)', false)).toBe('Building…')
    expect(cardLabel({ service_id: 's', status: 'running' }, 'Deploying…', false)).toBe('Running')
    expect(cardLabel({ service_id: 's', status: 'deleting' }, null, false)).toBe('Deleting…')
    expect(cardLabel({ service_id: 's', status: 'unknown' }, null, false)).toBe('Unknown')
    expect(cardLabel(null, 'Generating…', false)).toBe('Generating…')
    expect(cardLabel(null, 'Paused (input)', false)).toBe('Paused (input)')
    expect(cardLabel(null, 'Refused (capacity)', false)).toBe('Refused (capacity)')
    expect(cardLabel(null, 'Ready to deploy', false)).toBe('Ready to deploy')
    expect(cardLabel(null, 'Deploying…', false)).toBe('Deploying…')
    expect(cardLabel({ service_id: 's', status: 'running' }, null, true)).toBeNull()
  })
})
