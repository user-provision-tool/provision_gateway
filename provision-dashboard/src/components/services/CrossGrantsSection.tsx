import { useEffect, useState } from 'react'
import { Space, Tag, Select, Button, message, Typography } from 'antd'
import client from '../../api/client'

const { Text } = Typography

/**
 * G83/G84/G86 — the service card's cross-service section.
 *
 * The admin assigns; the owner sees read-only; a granted viewer sees neither
 * grants nor keys (the card is hidden for them by the page's access filter, and
 * this component re-checks the role).
 *
 * G86: the target picker groups services by OWNER ACCOUNT and lists only the
 * accounts the source owner can reach (own + active account grant + public).
 * The `Public` marker sits on the group header, never on a service. A `Can
 * call` item is clickable and opens the target's existing service card in a
 * popup (a view, not a second editor).
 *
 * G84: same-owner is the only implicit target; every cross-owner target —
 * public or private — needs the explicit row. A granted public target shows
 * reason `grant`, never `public`.
 */
export interface CrossGrantsSectionProps {
  serviceId: string
  isAdmin: boolean
  isOwner: boolean
  onPeek?: (serviceId: string) => void
}

export default function CrossGrantsSection({
  serviceId, isAdmin, isOwner, onPeek,
}: CrossGrantsSectionProps) {
  const [data, setData] = useState<any>(null)
  const [candidates, setCandidates] = useState<any[]>([])
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const visible = isAdmin || isOwner

  const load = async () => {
    if (!visible) return
    try {
      const { data: body } = await client.get(`/service-grants/${serviceId}`)
      setData(body)
    } catch {
      setData(null)
    }
    if (isAdmin) {
      try {
        const { data: body } = await client.get(`/service-grants/${serviceId}/candidates`)
        setCandidates(body.candidates || [])
      } catch {
        setCandidates([])
      }
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serviceId, visible, isAdmin])

  if (!visible || !data) return null

  const effective: any[] = data.effective || []
  // G95 R6 — a stale explicit row is VISIBLE and is not pre-selected: it can no
  // longer hold a permission, so keeping it in the picker's value is exactly
  // what stopped a valid target from being added (B4).
  const explicitRows: any[] = data.explicit || []
  const staleRows = explicitRows.filter((r: any) => r.stale)
  const explicit: string[] = explicitRows
    .filter((r: any) => !r.stale)
    .map((r: any) => r.target_service_id)

  const syncMessage = (sync: any) => {
    // R4 — a durable grant can still be deferred; the click must say so.
    if (!sync) return null
    if (sync.outcome === 'deferred') {
      return `Grant saved; the resolver sync was deferred — ${sync.reason || 'the next sync cycle retries it'}`
    }
    if (sync.outcome === 'error') {
      return `Grant saved; the resolver sync failed — ${sync.reason || 'the global reconciler will retry'}`
    }
    return null
  }

  // G102 — group the grantable candidates by owner account.  Same-owner entries
  // are ordinary, grantable rows too: they are offered in the editor like any
  // cross-owner candidate.
  const groups: { key: string; label: string; options: { value: string; label: string }[] }[] = []
  const byAccount = new Map<string, { key: string; label: string; options: { value: string; label: string }[] }>()
  for (const c of candidates) {
    const key = c.user_name || '(unknown)'
    let group = byAccount.get(key)
    if (!group) {
      group = {
        key,
        // G86 — Public is an ACCOUNT marker; it is not repeated per service.
        label: `${key}${c.is_public ? ' · Public' : ''}`,
        options: [],
      }
      byAccount.set(key, group)
      groups.push(group)
    }
    group.options.push({ value: c.service_id, label: `${c.service_name}:${c.label}` })
  }

  const save = async (next: string[]) => {
    // R10 — the desired set is ONE transaction: the server validates every
    // target and applies add/remove together (all-or-nothing).
    setSaving(true)
    try {
      const { data: body } = await client.put(
        `/service-grants/${serviceId}`, { target_service_ids: next })
      const warning = syncMessage(body.sync)
      if (warning) message.warning(warning)
      else message.success('Service grants updated')
      await load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || 'Could not update the grants')
    } finally {
      setSaving(false)
    }
  }

  const removeStale = async (targetId: string) => {
    // R6 — one action removes a row that no longer holds a permission.
    setSaving(true)
    try {
      const { data: body } = await client.delete(
        `/service-grants/${serviceId}/${targetId}`)
      const warning = syncMessage(body.sync)
      if (warning) message.warning(warning)
      else message.success('Stale grant removed')
      await load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || 'Could not remove the stale grant')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      <Text strong>Can call: </Text>
      {effective.length === 0 ? (
        <Text type="secondary">none</Text>
      ) : (
        <Space wrap size={4}>
          {effective.map((t: any) => (
            <Tag
              key={t.service_id}
              color={t.reason === 'grant' ? 'gold' : 'blue'}
              style={{ cursor: onPeek ? 'pointer' : undefined }}
              title={onPeek ? "Open the target's service card" : undefined}
              onClick={() => onPeek?.(t.service_id)}
            >
              {t.user_name}:{t.service_name}:{t.label}
              <span style={{ fontSize: 11, opacity: 0.7 }}> · {t.reason}</span>
            </Tag>
          ))}
        </Space>
      )}
      {isAdmin && (
        <Button
          size="small"
          type="link"
          style={{ padding: '0 6px' }}
          loading={saving}
          onClick={() => setEditing(!editing)}
        >
          {editing ? 'Done' : 'Edit'}
        </Button>
      )}
      {isAdmin && editing && (
        <div style={{ marginTop: 6 }}>
          <Select
            mode="multiple"
            style={{ width: '100%' }}
            value={explicit}
            options={groups}
            placeholder="Select cross-owner targets"
            onChange={save}
            disabled={saving}
          />
          {/* R6 — rows that no longer hold a permission stay visible, marked,
              and are removed in one click. They are NOT in the Select's value,
              so they never block a valid target from being added. */}
          {staleRows.length > 0 && (
            <div style={{ marginTop: 6 }}>
              {staleRows.map((r: any) => (
                <Space key={r.target_service_id} size={4} style={{ display: 'flex' }}>
                  <Tag color="red">
                    {r.target
                      ? `${r.target.user_name}:${r.target.service_name}:${r.target.label}`
                      : r.target_service_id}
                    {' — '}{r.stale_message || 'no longer permitted'}
                  </Tag>
                  <Button size="small" danger loading={saving}
                    onClick={() => removeStale(r.target_service_id)}>
                    Remove
                  </Button>
                </Space>
              ))}
            </div>
          )}
          <div style={{ fontSize: 12, color: '#999', marginTop: 4 }}>
            Only accounts this owner can reach are listed (own, an active
            account grant, or a public account). Every target, same-owner
            included, needs this row; a private target also needs an active
            account grant.
          </div>
        </div>
      )}
    </div>
  )
}
