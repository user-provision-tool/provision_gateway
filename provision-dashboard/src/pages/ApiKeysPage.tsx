import { useState, useEffect, useCallback } from 'react'
import { Typography, Card, Table, Button, Space, Tag, Modal, Input, message, Popconfirm, Tooltip, Select } from 'antd'
import { PlusOutlined, DeleteOutlined, CopyOutlined, StarOutlined } from '@ant-design/icons'
import { useAuth } from '../hooks/useAuth'
import client from '../api/client'

const { Title, Text } = Typography

interface KeyOwner { username: string | null; role: string | null }
interface ApiKey {
  id: number
  user_id: string
  owner?: KeyOwner
  label: string
  mask: string | null
  is_default: boolean
  created_at: string | null
  expires_at: string | null
  is_revoked: boolean
  last_used_at: string | null
}

export default function ApiKeysPage() {
  const { admin } = useAuth()
  const [keys, setKeys] = useState<ApiKey[]>([])
  const [owners, setOwners] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [createOpen, setCreateOpen] = useState(false)
  const [newLabel, setNewLabel] = useState('')
  const [newUserId, setNewUserId] = useState('')
  const [createLoading, setCreateLoading] = useState(false)
  const [createdToken, setCreatedToken] = useState<string | null>(null)

  const isAdmin = admin?.role === 'admin'

  const openCreate = () => {
    setNewLabel(''); setNewUserId(''); setCreatedToken(null); setCreateOpen(true)
  }
  const closeCreate = () => {
    setNewLabel(''); setNewUserId(''); setCreatedToken(null); setCreateOpen(false)
  }

  const fetchKeys = useCallback(async () => {
    try {
      const { data } = await client.get('/auth/keys')
      setKeys(data.keys || [])
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to load API keys')
    } finally { setLoading(false) }
  }, [])

  const fetchOwners = useCallback(async () => {
    if (!isAdmin) return
    try {
      const { data } = await client.get('/auth/users')
      setOwners((data.users || []).filter((u: any) => u.role !== 'special' && !u.is_deleted))
    } catch { /* owner picker is optional */ }
  }, [isAdmin])

  useEffect(() => { fetchKeys(); fetchOwners() }, [fetchKeys, fetchOwners])

  const handleCreate = async () => {
    if (!newLabel.trim()) { message.warning('Enter a label'); return }
    setCreateLoading(true)
    try {
      const payload: any = { label: newLabel.trim() }
      if (isAdmin && newUserId.trim()) payload.user_id = newUserId.trim()
      const { data } = await client.post('/auth/keys', payload)
      message.success('API key created')
      setCreatedToken(data.token)
      setNewLabel(''); setNewUserId('')
      fetchKeys()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to create key')
    } finally { setCreateLoading(false) }
  }

  const handleRevoke = async (keyId: number) => {
    try { await client.delete(`/auth/keys/${keyId}`); message.success('Key revoked'); fetchKeys() }
    catch (err: any) { message.error(err.response?.data?.detail || 'Failed to revoke key') }
  }

  const handleSetDefault = async (keyId: number) => {
    try { await client.put(`/auth/keys/${keyId}/default`); message.success('Default key updated'); fetchKeys() }
    catch (err: any) { message.error(err.response?.data?.detail || 'Failed to set default key') }
  }

  const ownerLabel = (record: ApiKey) =>
    record.owner?.username ? `${record.owner.username}:${record.owner.role}` : '-'

  const columns = (showOwner: boolean) => [
    { title: 'ID', dataIndex: 'id', key: 'id', width: 60 },
    ...(showOwner ? [{ title: 'Owner', key: 'owner', width: 180,
      render: (_: any, record: ApiKey) => <Text code>{ownerLabel(record)}</Text> }] : []),
    { title: 'Label', dataIndex: 'label', key: 'label' },
    { title: 'Mask', dataIndex: 'mask', key: 'mask', width: 100,
      render: (v: string | null) => v ? <Text code>{v}</Text> : '-' },
    { title: 'Default', dataIndex: 'is_default', key: 'is_default', width: 90,
      render: (v: boolean) => v ? <Tag color="gold" icon={<StarOutlined />}>Default</Tag> : <Text type="secondary">-</Text> },
    { title: 'Created', dataIndex: 'created_at', key: 'created_at',
      render: (v: string | null) => v ? new Date(v).toLocaleString() : '-' },
    { title: 'Expires', dataIndex: 'expires_at', key: 'expires_at',
      render: (v: string | null) => v ? new Date(v).toLocaleString() : '-' },
    { title: 'Status', dataIndex: 'is_revoked', key: 'is_revoked',
      render: (v: boolean) => v ? <Tag color="red">Revoked</Tag> : <Tag color="green">Active</Tag> },
    { title: 'Actions', key: 'actions', width: 220,
      render: (_: any, record: ApiKey) => !record.is_revoked ? (
        <Space>
          {!record.is_default && (
            <Tooltip title="Use this key by default for new sessions">
              <Button size="small" icon={<StarOutlined />} onClick={() => handleSetDefault(record.id)}>Default</Button>
            </Tooltip>
          )}
          <Popconfirm title="Revoke this key?" onConfirm={() => handleRevoke(record.id)}>
            <Button size="small" danger icon={<DeleteOutlined />}>Revoke</Button>
          </Popconfirm>
        </Space>
      ) : null },
  ]

  const mine = keys.filter((k) => k.user_id === admin?.id)
  const all = keys

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16, flexWrap: 'wrap', gap: 8 }}>
        <Title level={3} style={{ margin: 0 }}>API Keys</Title>
        <Space>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>Create Key</Button>
          <Button onClick={fetchKeys}>Refresh</Button>
        </Space>
      </div>

      <Title level={5}>My API Keys</Title>
      <Card style={{ marginBottom: 16 }}>
        <Table dataSource={mine} columns={columns(false)} rowKey="id" loading={loading}
               pagination={false} size="small" />
      </Card>

      {isAdmin && (
        <>
          <Title level={5}>API Keys <Text type="secondary" style={{ fontSize: 12, fontWeight: 'normal' }}>
            — every viewer's and admin's keys</Text></Title>
          <Card>
            <Table dataSource={all} columns={columns(true)} rowKey="id" loading={loading}
                   pagination={false} size="small" />
          </Card>
        </>
      )}

      <Modal title="Create API Key" open={createOpen} onCancel={closeCreate}
        footer={createdToken ? [<Button key="close" onClick={closeCreate}>Close</Button>] : [
          <Button key="cancel" onClick={closeCreate}>Cancel</Button>,
          <Button key="create" type="primary" loading={createLoading} onClick={handleCreate}>Create</Button>,
        ]}>
        {createdToken ? (
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <Text type="success" strong>API Key created successfully!</Text>
            <Text type="warning">Copy this token now — it will not be shown again.</Text>
            <Input.TextArea value={createdToken} rows={3} readOnly
              style={{ fontFamily: 'monospace', fontSize: 12 }} />
            <Button icon={<CopyOutlined />} onClick={() => {
              navigator.clipboard.writeText(createdToken)
              message.success('Copied to clipboard')
            }}>Copy Token</Button>
          </Space>
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <div>
              <Text strong>Label: </Text>
              <Input placeholder="e.g. Production, CI/CD" value={newLabel} onChange={e => setNewLabel(e.target.value)} />
            </div>
            {isAdmin && (
              <div>
                <Text strong>Owner (optional): </Text>
                <Select
                  allowClear
                  style={{ width: '100%' }}
                  placeholder="blank = your own key"
                  value={newUserId || undefined}
                  onChange={(v) => setNewUserId(v || '')}
                  options={owners.map((u: any) => ({
                    value: u.id, label: `${u.username}:${u.role}`,
                  }))}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  Choose a viewer or admin. A special functional user never logs in and has no keys.
                </Text>
              </div>
            )}
            <Text type="secondary">Keys are valid for 1 year.</Text>
          </Space>
        )}
      </Modal>
    </div>
  )
}
