import { describe, expect, it } from 'vitest'
import {
  addPlaceholder, apiKeyScopes, batchBlocking, batchEnabled, cardBusy, cardKey,
  cardLabel, formatEta, instanceRoutes, ownerLabel, ownerTags, pendingLabel,
  placeholderKey, queueLabel, reconcilePlaceholders, statusBadge, targetDisplay,
  taskFamily, taskLogRoute, taskRoute,
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

describe('instant feedback (GRILLING G90)', () => {
  it('maps every pending code to its click-time label', () => {
    expect(pendingLabel('starting')).toBe('Starting…')
    expect(pendingLabel('stopping')).toBe('Stopping…')
    expect(pendingLabel('deleting')).toBe('Deleting…')
    expect(pendingLabel('cancelling')).toBe('Cancelling…')
    expect(pendingLabel('removing')).toBe('Removing…')
    expect(pendingLabel(null)).toBeNull()
    expect(pendingLabel(undefined)).toBeNull()
  })

  it('lets the operator-pending state win over failed and building', () => {
    expect(statusBadge('failed', 'deleting', { failedStage: 'build' })?.text).toBe('Deleting…')
    expect(statusBadge('building', 'cancelling')?.text).toBe('Cancelling…')
    expect(statusBadge('failed', 'starting')?.tone).toBe('processing')
    expect(statusBadge('unknown', 'stopping')?.text).toBe('Stopping…')
  })

  it('keeps the server badge when no operation is pending', () => {
    expect(statusBadge('failed', null, { failedStage: 'compose' }))
      .toEqual({ tone: 'error', text: 'Failed (compose)' })
    expect(statusBadge('failed', null, { analyzing: true })?.text).toBe('Failed + analyzing')
    expect(statusBadge('building', undefined)?.text).toBe('Building...')
    expect(statusBadge('deleting', undefined)?.text).toBe('Deleting...')
    // `running`/`unknown` need the container counts; the helper declines.
    expect(statusBadge('running', undefined)).toBeNull()
    expect(statusBadge(undefined, null)).toBeNull()
  })

  it('keys a placeholder by user + service + label, one per triple', () => {
    expect(placeholderKey('alice', 'example-s2s', 3)).toBe('alice::example-s2s::3')
    const one = addPlaceholder([], 'alice', 'example-s2s', 3)
    expect(one).toHaveLength(1)
    expect(addPlaceholder(one, 'alice', 'example-s2s', '3')).toBe(one)
    expect(addPlaceholder(one, 'alice', 'example-s2s', 4)).toHaveLength(2)
  })

  it('shows the queued and planned waiting states', () => {
    expect(statusBadge('queued', undefined)).toEqual({ tone: 'default', text: 'Queued' })
    expect(statusBadge('planned', undefined)).toEqual({ tone: 'processing', text: 'Planned' })
    expect(cardBusy('queued') && cardBusy('planned') && cardBusy('building')).toBe(true)
    expect(cardBusy('running')).toBe(false)
  })

  it('labels a queued card with its position and approximate ETA', () => {
    expect(queueLabel({ status: 'queued', position: 3, eta_start_s: 120 }))
      .toBe('3 ahead · ~2m')
    expect(queueLabel({ status: 'planned' })).toBe('planned')
    expect(queueLabel(null)).toBeNull()
    expect(formatEta(45)).toBe('45s')
    expect(formatEta(3720)).toBe('1h 2m')
  })

  it('pre-filters a batch: every selected card must be in the allowed set', () => {
    expect(batchEnabled('stop', ['running', 'running'])).toBe(true)
    expect(batchEnabled('stop', ['running', 'building'])).toBe(false)
    expect(batchBlocking('stop', ['running', 'building', 'queued']))
      .toEqual(['building', 'queued'])
    // A building/queued/planned card allows no destructive batch action.
    expect(batchBlocking('delete', ['queued'])).toEqual(['queued'])
    expect(batchBlocking('rebuild', ['building'])).toEqual(['building'])
    expect(batchEnabled('start', [])).toBe(false)
  })

  it('replaces a placeholder when the stages refresh returns its triple', () => {
    const list = [
      ...addPlaceholder([], 'alice', 'example-s2s', 3),
      ...addPlaceholder([], 'bob', 'example-s2s', 3),
    ]
    const services = [{ user_name: 'alice', service_name: 'example-s2s', label: 3 }]
    const after = reconcilePlaceholders(list, services, [])
    expect(after.map(p => p.key)).toEqual(['bob::example-s2s::3'])
    // A pre-deploy session counts too — the card exists before registration.
    const sessions = [{ user_name: 'bob', service_name: 'example-s2s', label: '3' }]
    expect(reconcilePlaceholders(list, [], sessions).map(p => p.key))
      .toEqual(['alice::example-s2s::3'])
    // No matching entry: the list is returned unchanged (same reference).
    expect(reconcilePlaceholders(list, [], [])).toBe(list)
  })
})
