import { useState, useEffect, useCallback } from 'react'
import { Typography, Card, Table, Button, Space, Tag, Modal, Input, message, Popconfirm, Tooltip } from 'antd'
import { PlusOutlined, DeleteOutlined, KeyOutlined, CopyOutlined, StarOutlined } from '@ant-design/icons'
import { useAuth } from '../hooks/useAuth'
import client from '../api/client'

const { Title, Text } = Typography

interface ApiKey {
  id: number
  user_id: number
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
      if (isAdmin && newUserId) {
        payload.user_id = parseInt(newUserId)
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
    ...(isAdmin ? [{ title: 'User ID', dataIndex: 'user_id', key: 'user_id', width: 80 }] : []),
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
          <Button type="primary" icon={<PlusOutlined />} onClick={() => { setCreateOpen(true); setCreatedToken(null) }}>Create Key</Button>
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
        onCancel={() => { setCreateOpen(false); setCreatedToken(null) }}
        footer={createdToken ? [
          <Button key="close" onClick={() => { setCreateOpen(false); setCreatedToken(null) }}>Close</Button>
        ] : [
          <Button key="cancel" onClick={() => setCreateOpen(false)}>Cancel</Button>,
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
                <Text strong>User ID (optional): </Text>
                <Input placeholder="Leave blank for your own keys" value={newUserId} onChange={e => setNewUserId(e.target.value)} />
              </div>
            )}
            <Text type="secondary">Keys are valid for 1 year. You can create multiple keys with different labels for different purposes.</Text>
          </Space>
        )}
      </Modal>
    </div>
  )
}
