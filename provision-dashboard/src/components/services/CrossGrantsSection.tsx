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
  const explicit: string[] = (data.explicit || []).map((r: any) => r.target_service_id)

  // G86 — group the grantable candidates by owner account. Same-owner entries
  // are implicit (no row is ever taken for them), so they are not offered in
  // the editor; the effective list above still shows them.
  const groups: { key: string; label: string; options: { value: string; label: string }[] }[] = []
  const byAccount = new Map<string, { key: string; label: string; options: { value: string; label: string }[] }>()
  for (const c of candidates) {
    if (c.implicit) continue
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
      await client.put(`/service-grants/${serviceId}`, { target_service_ids: next })
      message.success('Service grants updated')
      await load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || 'Could not update the grants')
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
          <div style={{ fontSize: 12, color: '#999', marginTop: 4 }}>
            Only accounts this owner can reach are listed (own, an active
            account grant, or a public account). Same-owner targets are implicit
            and are never listed here. A cross-owner target always needs this
            row; a private one also needs an active account grant.
          </div>
        </div>
      )}
    </div>
  )
}
