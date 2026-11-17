import client from './client'

export interface AlertDto {
  id: string
  source_key?: string | null
  kind: 'event' | 'condition'
  condition_key?: string | null
  status: 'active' | 'resolved'
  type: string
  severity: string
  title: string
  message: string
  task_id?: string | null
  service_id?: string | null
  user_name?: string | null
  service_name?: string | null
  label?: string | null
  context?: Record<string, any>
  created_at?: string | null
  updated_at?: string | null
  last_seen_at?: string | null
  resolved_at?: string | null
  read_at?: string | null
  read_by?: string | null
  read_by_name?: string | null
  read: boolean
}

export interface AlertList {
  alerts: AlertDto[]
  total: number
  unread: number
  limit: number
  offset: number
}

export interface AlertCounts {
  unread: number
  total: number
  active: number
}

export interface AlertFilters {
  q?: string
  type?: string
  kind?: string
  status?: 'active' | 'resolved'
  read?: 'read' | 'unread'
  limit?: number
  offset?: number
}

// The ONE type → label map. An unknown key falls back to the key itself, so a
// new alert type renders without a page change (R8).
export const ALERT_TYPE_LABELS: Record<string, string> = {
  stream_conf_hold_collision: 'Stream conf hold collision',
  orphan_user_services: 'Orphan user services',
  acl_drift: 'ACL drift',
  grant_sync_failed: 'Grant sync failed',
  service_failed: 'Service failed',
}

export const alertTypeLabel = (type?: string | null): string =>
  (type && ALERT_TYPE_LABELS[type]) || type || 'Unknown'

export const listAlerts = (params: AlertFilters = {}) =>
  client.get('/alerts', { params })

export const getAlertCounts = () => client.get('/alerts/count')

export const markRead = (ids: string[]) => client.post('/alerts/read', { ids })
export const markUnread = (ids: string[]) => client.post('/alerts/unread', { ids })
export const markAllRead = () => client.post('/alerts/read-all')
