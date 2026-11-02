/** Pure identity / task / owner / key / route contracts (T10, Vitest).
 *
 * These are the rules the UI must apply; keeping them pure makes them testable
 * without a browser and prevents a component from inventing a second rule.
 */

export const cardKey = (serviceId: string) => `svc:${serviceId}`

export interface OwnerDto {
  role?: string | null
  is_public_shared?: boolean
  was_public?: boolean
  has_gateway_user?: boolean
  deleted_at?: string | null
}

export interface ServiceCard {
  service_id: string
  status?: string | null
  has_auth?: boolean
}

export const ownerTags = (o: OwnerDto | null | undefined): string[] => {
  if (!o) return []
  const tags: string[] = []
  if (o.role === 'special') tags.push('Functional')
  if ((o.role === 'special') && (o.is_public_shared || o.was_public)) tags.push('Public')
  if (o.has_gateway_user === false && !o.deleted_at) tags.push('Service-only')
  if (o.deleted_at) tags.push('owner gone')
  return tags
}

export type TaskFamily = 'gateway' | 'api'
export const taskFamily = (taskId: string): TaskFamily =>
  taskId.startsWith('gt-') ? 'gateway' : 'api'

export const targetDisplay = (serviceName?: string | null, recipePath?: string | null): string => {
  if (!serviceName) return ''
  const recipe = (recipePath || '').trim()
  return recipe && recipe !== '.' ? `${serviceName} (${recipe})` : serviceName
}

export type Role = 'viewer' | 'special' | 'admin'
export const apiKeyScopes = (role: Role | undefined): string[] => {
  if (role === 'admin') return ['My API Keys', 'API Keys']
  if (role === 'viewer') return ['My API Keys']
  return []
}

export const ownerLabel = (username?: string | null, role?: string | null): string =>
  username ? `${username}:${role || ''}` : '-'

export const instanceRoutes = (serviceId: string) => ({
  get: `/api/services/instances/${serviceId}`,
  remove: (scope: 'instance' | 'service' = 'service') =>
    `/api/services/instances/${serviceId}?scope=${scope}`,
  stop: `/api/services/instances/${serviceId}/stop`,
  start: `/api/services/instances/${serviceId}/start`,
  rebuild: `/api/services/instances/${serviceId}/rebuild`,
  password: `/api/services/instances/${serviceId}/password`,
})

export const taskRoute = (taskId: string) => `/tasks/${taskId}`
export const taskLogRoute = (taskId: string) => `/api/tasks/${taskId}/log`

/** The one-card merge: instance base; only a failed instance layers the session;
 *  a tombstone has no card; a session is suppressed otherwise. */
export const cardLabel = (
  service: ServiceCard | null,
  sessionLabel: string | null,
  tombstoned: boolean,
): string | null => {
  if (tombstoned || !service) return sessionLabel
  const status = service.status || 'unknown'
  if (status === 'failed') return sessionLabel ? `Failed + ${sessionLabel}` : 'Failed'
  if (status === 'building') return 'Building…'
  if (status === 'deleting') return 'Deleting…'
  if (status === 'unknown') return 'Unknown'
  return 'Running'
}
