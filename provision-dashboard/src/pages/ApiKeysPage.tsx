import { useState, useEffect, useCallback } from 'react'
import { Typography, Card, Table, Button, Space, Tag, Modal, Input, message, Popconfirm, Tooltip } from 'antd'
import { PlusOutlined, DeleteOutlined, KeyOutlined, CopyOutlined, StarOutlined } from '@ant-design/icons'
import { useAuth } from '../hooks/useAuth'
import client from '../api/client'

const { Title, Text } = Typography

interface ApiKey {
  id: number
  user_id: number
  // Which id space `user_id` names: 'end_user' | 'admin'. The two tables have
  // separate ids, so the pair — not the number alone — identifies the target.
  target_type: string
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
  const [loading, setLoading] = useState(true)
  const [createOpen, setCreateOpen] = useState(false)
  const [newLabel, setNewLabel] = useState('')
  const [newUserId, setNewUserId] = useState('')
  const [createLoading, setCreateLoading] = useState(false)
  const [createdToken, setCreatedToken] = useState<string | null>(null)

  // Opening or leaving the dialog starts a FRESH form. Without this, a cancelled
  // (or failed) create leaves its Label/Target behind, and the next create
  // silently reuses them — e.g. a Target left as `service:1` re-sends that 400.
  const openCreate = () => {
    setNewLabel('')
    setNewUserId('')
    setCreatedToken(null)
    setCreateOpen(true)
  }
  const closeCreate = () => {
    setNewLabel('')
    setNewUserId('')
    setCreatedToken(null)
    setCreateOpen(false)
  }

  const isAdmin = admin?.role === 'admin'

  const fetchKeys = useCallback(async () => {
    try {
      const { data } = await client.get('/auth/keys')
      setKeys(data.keys || [])
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to load API keys')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchKeys() }, [fetchKeys])

  const handleCreate = async () => {
    if (!newLabel.trim()) { message.warning('Enter a label'); return }
    setCreateLoading(true)
    try {
      const payload: any = { label: newLabel.trim() }
      if (isAdmin && newUserId.trim()) {
        // A PREFIXED target reference (`end_user:7` / `admin:1`) — sent as typed,
        // because the prefix is what names the id space. A bare number means an
        // end user; blank means "me, in my own space" (no user_id sent at all).
        payload.user_id = newUserId.trim()
      }
      const { data } = await client.post('/auth/keys', payload)
      message.success('API key created')
      setCreatedToken(data.token)
      setNewLabel('')
      setNewUserId('')
      fetchKeys()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to create key')
    } finally {
      setCreateLoading(false)
    }
  }

  const handleRevoke = async (keyId: number) => {
    try {
      await client.delete(`/auth/keys/${keyId}`)
      message.success('Key revoked')
      fetchKeys()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to revoke key')
    }
  }

  const handleSetDefault = async (keyId: number) => {
    try {
      await client.put(`/auth/keys/${keyId}/default`)
      message.success('Default key updated')
      fetchKeys()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to set default key')
    }
  }

  const columns = [
    { title: 'ID', dataIndex: 'id', key: 'id', width: 60 },
    { title: 'Label', dataIndex: 'label', key: 'label' },
    ...(isAdmin ? [{ title: 'Target', dataIndex: 'user_id', key: 'user_id', width: 130,
      render: (_: number, record: ApiKey) => (
        <Text code>{record.target_type || 'end_user'}:{record.user_id}</Text>
      ) }] : []),
    { title: 'Mask', dataIndex: 'mask', key: 'mask', width: 90,
      render: (v: string | null) => v ? <Text code>{v}</Text> : '-' },
    { title: 'Default', dataIndex: 'is_default', key: 'is_default', width: 90,
      render: (v: boolean) => v
        ? <Tag color="gold" icon={<StarOutlined />}>Default</Tag>
        : <Text type="secondary">-</Text> },
    { title: 'Created', dataIndex: 'created_at', key: 'created_at',
      render: (v: string | null) => v ? new Date(v).toLocaleString() : '-' },
    { title: 'Expires', dataIndex: 'expires_at', key: 'expires_at',
      render: (v: string | null) => v ? new Date(v).toLocaleString() : '-' },
    { title: 'Status', dataIndex: 'is_revoked', key: 'is_revoked',
      render: (v: boolean) => v ? <Tag color="red">Revoked</Tag> : <Tag color="green">Active</Tag> },
    { title: 'Actions', key: 'actions', width: 220,
      render: (_: any, record: ApiKey) => (
        !record.is_revoked ? (
          <Space>
            {!record.is_default && (
              <Tooltip title="Use this key by default for new sessions">
                <Button size="small" icon={<StarOutlined />} onClick={() => handleSetDefault(record.id)}>
                  Set as Default
                </Button>
              </Tooltip>
            )}
            <Popconfirm title="Revoke this key?" onConfirm={() => handleRevoke(record.id)}>
              <Button size="small" danger icon={<DeleteOutlined />}>Revoke</Button>
            </Popconfirm>
          </Space>
        ) : null
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16, flexWrap: 'wrap', gap: 8 }}>
        <Title level={3} style={{ margin: 0 }}>API Keys</Title>
        <Space>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>Create Key</Button>
          <Button onClick={fetchKeys}>Refresh</Button>
        </Space>
      </div>

      <Card>
        <Table
          dataSource={keys}
          columns={columns}
          rowKey="id"
          loading={loading}
          pagination={false}
          size="small"
        />
      </Card>

      <Modal
        title="Create API Key"
        open={createOpen}
        onCancel={closeCreate}
        footer={createdToken ? [
          <Button key="close" onClick={closeCreate}>Close</Button>
        ] : [
          <Button key="cancel" onClick={closeCreate}>Cancel</Button>,
          <Button key="create" type="primary" loading={createLoading} onClick={handleCreate}>Create</Button>
        ]}
      >
        {createdToken ? (
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <Text type="success" strong>API Key created successfully!</Text>
            <Text type="warning">Copy this token now — it will not be shown again.</Text>
            <Input.TextArea
              value={createdToken}
              rows={3}
              readOnly
              style={{ fontFamily: 'monospace', fontSize: 12 }}
            />
            <Button
              icon={<CopyOutlined />}
              onClick={() => {
                navigator.clipboard.writeText(createdToken)
                message.success('Copied to clipboard')
              }}
            >
              Copy Token
            </Button>
          </Space>
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <div>
              <Text strong>Label: </Text>
              <Input placeholder="e.g. Production, CI/CD" value={newLabel} onChange={e => setNewLabel(e.target.value)} />
            </div>
            {isAdmin && (
              <div>
                <Text strong>Target (optional): </Text>
                <Input placeholder="blank = your own keys, or admin:1 / end_user:7"
                       value={newUserId} onChange={e => setNewUserId(e.target.value)} />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  Prefix the id with its kind: <Text code>admin:1</Text> or <Text code>end_user:7</Text>.
                  A bare number is an end user. Blank creates the key for you — in your own id space.
                </Text>
              </div>
            )}
            <Text type="secondary">Keys are valid for 1 year. You can create multiple keys with different labels for different purposes.</Text>
          </Space>
        )}
      </Modal>
    </div>
  )
}
