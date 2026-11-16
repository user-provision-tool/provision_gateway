/** Pure identity / task / owner / key / route contracts (T10, Vitest).
 *
 * These are the rules the UI must apply; keeping them pure makes them testable
 * without a browser and prevents a component from inventing a second rule.
 */

export const cardKey = (serviceId: string) => `svc:${serviceId}`

// ---- Instant feedback (GRILLING G90, 2026-11-08) ---------------------------
//
// A status-changing click is acknowledged AT ONCE by a client-side pending
// code; completion may take longer. The pending label is checked BEFORE the
// server status, so a delete on a `failed` or `building` card is visible in the
// same render as the click. None of these codes is a server state.

export type PendingCode =
  | 'starting' | 'stopping' | 'deleting' | 'cancelling' | 'removing'

const PENDING_LABELS: Record<PendingCode, string> = {
  starting: 'Starting…',
  stopping: 'Stopping…',
  deleting: 'Deleting…',
  cancelling: 'Cancelling…',
  removing: 'Removing…',
}

export const pendingLabel = (code?: PendingCode | null): string | null =>
  code ? PENDING_LABELS[code] || null : null

export type BadgeTone = 'error' | 'processing' | 'success' | 'warning' | 'default'

/** The status-only badge. The operator-pending code wins over every server
 *  status (G90 badge precedence); `null` means "no status-only answer" and the
 *  caller falls back to the container counts. */
export const statusBadge = (
  status: string | undefined,
  pending?: PendingCode | null,
  opts?: { analyzing?: boolean; failedStage?: string },
): { tone: BadgeTone; text: string } | null => {
  const label = pendingLabel(pending)
  if (label) return { tone: 'processing', text: label }
  if (status === 'failed') {
    const stage = opts?.failedStage ? ` (${opts.failedStage})` : ''
    return opts?.analyzing
      ? { tone: 'processing', text: `Failed${stage} + analyzing` }
      : { tone: 'error', text: `Failed${stage}` }
  }
  if (status === 'queued') return { tone: 'default', text: 'Queued' }
  if (status === 'planned') return { tone: 'processing', text: 'Planned' }
  if (status === 'building') return { tone: 'processing', text: 'Building...' }
  if (status === 'deleting') return { tone: 'processing', text: 'Deleting...' }
  return null
}

// ---- Card / batch operation matrix (G92 R8; GRILLING G93) -------------------
//
// One home for "which operations a card in status X allows". A `building`
// card offers CANCEL ONLY (the server refuses delete/rebuild with 409, so the
// UI must not offer them); `queued`/`planned` behave the same because their
// work has not started. Batches enable an action only when EVERY selected card
// is in that action's allowed set.

export type BatchAction = 'start' | 'stop' | 'rebuild' | 'delete'

export const CARD_ACTION_ALLOWED: Record<BatchAction, ReadonlySet<string>> = {
  start: new Set(['running']),
  stop: new Set(['running']),
  rebuild: new Set(['running', 'failed']),
  delete: new Set(['running', 'failed', 'unknown']),
}

/** `null` = enabled; otherwise the blocking statuses, for the tooltip. */
export const batchBlocking = (
  action: BatchAction, statuses: (string | null | undefined)[],
): string[] => {
  const allowed = CARD_ACTION_ALLOWED[action]
  const blocking = new Set<string>()
  for (const status of statuses) {
    const s = status || 'unknown'
    if (!allowed.has(s)) blocking.add(s)
  }
  return Array.from(blocking)
}

export const batchEnabled = (
  action: BatchAction, statuses: (string | null | undefined)[],
): boolean => statuses.length > 0 && batchBlocking(action, statuses).length === 0

/** The Cancel-only card action set while work is waiting or building. */
export const cardBusy = (status?: string | null): boolean =>
  status === 'building' || status === 'queued' || status === 'planned'
  || status === 'deleting'

export interface QueueInfo {
  status?: string
  /** `operator` is the only lane a card ever shows (G94 R5.3). */
  lane?: string
  position?: number | null
  eta_start_s?: number | null
  eta_finish_s?: number | null
  plan_seq?: number | null
}

/** Human-readable waiting text: queue position and approximate ETA (R7.2). */
export const queueLabel = (q?: QueueInfo | null): string | null => {
  if (!q || !q.status) return null
  if (q.status === 'queued') {
    const pos = q.position == null ? '' : `${q.position} ahead`
    const eta = q.eta_start_s != null ? ` · ~${formatEta(q.eta_start_s)}` : ''
    return pos ? `${pos}${eta}` : null
  }
  if (q.status === 'planned') return 'planned'
  return null
}

export const formatEta = (seconds: number): string => {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rem = s % 60
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

/** A placeholder card: the waiting card a card-creating operation inserts
 *  synchronously at submit, keyed by user + service + label. It is NOT a
 *  fabricated service status — it is replaced by the real entry. */
export interface PlaceholderCard {
  key: string
  user: string
  service: string
  label: string
}

export const placeholderKey = (
  user: string, service: string, label: string | number,
): string => `${user}::${service}::${label}`

export const addPlaceholder = (
  list: PlaceholderCard[], user: string, service: string, label: string | number,
): PlaceholderCard[] => {
  const key = placeholderKey(user, service, label)
  if (list.some(p => p.key === key)) return list
  return [...list, { key, user, service, label: String(label) }]
}

/** Drop a placeholder the moment the stages read model returns an instance or
 *  a pre-deploy session for its triple; keep every other placeholder. */
export const reconcilePlaceholders = (
  list: PlaceholderCard[],
  services: { user_name?: string; service_name: string; label: string | number }[],
  sessions: { user_name?: string; service_name: string; label: string | number }[],
): PlaceholderCard[] => {
  if (list.length === 0) return list
  const live = new Set<string>()
  for (const s of [...services, ...sessions]) {
    live.add(placeholderKey(s.user_name || 'unknown', s.service_name, String(s.label)))
  }
  const next = list.filter(p => !live.has(p.key))
  return next.length === list.length ? list : next
}

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
