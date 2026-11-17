import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  Alert, Badge, Button, Descriptions, Drawer, Empty, Input, Select, Space,
  Table, Tag, Typography, message,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { usePolling } from '../hooks/usePolling'
import {
  AlertDto, alertTypeLabel, listAlerts, markAllRead, markRead, markUnread,
} from '../api/alerts'

const { Text, Paragraph } = Typography

const SEVERITY_COLORS: Record<string, string> = {
  error: 'red', warning: 'orange', info: 'blue',
}

// A condition alert may key on a user rather than one service (the orphan
// condition is one row per username, with its affected services in context).
const affectedServices = (row: AlertDto): string => {
  const services = (row.context as any)?.services
  if (Array.isArray(services) && services.length > 0) {
    const names = services
      .map((s: any) => s?.service_name || s?.service_id)
      .filter(Boolean)
    return `${row.user_name || '?'}: ${names.join(', ')}`
  }
  const label = row.label != null && row.label !== '' ? `:${row.label}` : ''
  return `${row.user_name || '?'}:${row.service_name || '?'}${label}`
}

export default function AlertsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [rows, setRows] = useState<AlertDto[]>([])
  const [total, setTotal] = useState(0)
  const [unread, setUnread] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [q, setQ] = useState('')
  const [status, setStatus] = useState<'active' | 'resolved' | undefined>(undefined)
  const [limit, setLimit] = useState(50)
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<string[]>([])
  const [detail, setDetail] = useState<AlertDto | null>(null)

  const fetchAlerts = useCallback(async (opts: { silent?: boolean } = {}) => {
    if (!opts.silent) setLoading(true)
    try {
      const { data } = await listAlerts({ q: q || undefined, status, limit, offset })
      setRows(data.alerts || [])
      setTotal(data.total || 0)
      setUnread(data.unread || 0)
      setError(null)
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Could not load alerts')
    } finally {
      if (!opts.silent) setLoading(false)
    }
  }, [q, status, limit, offset])

  useEffect(() => { fetchAlerts() }, [fetchAlerts])
  usePolling(() => fetchAlerts({ silent: true }), 10000)

  // `/alerts?alert=<id>`: open the detail drawer for the alert a condition
  // banner linked to. The id lives in the newest 200 rows, which is enough for
  // a banner click; a missing id falls back to the normal list.
  const alertParam = searchParams.get('alert')
  useEffect(() => {
    if (!alertParam) return
    let cancelled = false
    ;(async () => {
      try {
        const { data } = await listAlerts({ limit: 200 })
        const hit = (data.alerts || []).find((a: AlertDto) => a.id === alertParam)
        if (!cancelled && hit) setDetail(hit)
      } catch { /* the list request above already reports errors */ }
    })()
    return () => { cancelled = true }
  }, [alertParam])

  const setRead = async (ids: string[], read: boolean) => {
    if (!ids.length) return
    try {
      if (read) await markRead(ids); else await markUnread(ids)
      message.success(read ? `Marked ${ids.length} read` : `Marked ${ids.length} unread`)
      setSelected([])
      fetchAlerts({ silent: true })
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to update read state')
    }
  }

  const readAll = async () => {
    try {
      const { data } = await markAllRead()
      message.success(`Marked ${data.updated} read`)
      fetchAlerts({ silent: true })
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to mark all read')
    }
  }

  const columns = useMemo(() => [
    {
      title: 'Type',
      dataIndex: 'type',
      width: 170,
      render: (type: string, row: AlertDto) => (
        <Space direction="vertical" size={0}>
          <Text>{alertTypeLabel(type)}</Text>
          <Tag color={SEVERITY_COLORS[row.severity] || 'default'}>{row.severity}</Tag>
        </Space>
      ),
    },
    {
      title: 'Title / message',
      dataIndex: 'title',
      render: (_: string, row: AlertDto) => (
        <Space direction="vertical" size={0} style={{ maxWidth: 420 }}>
          <Text strong>{row.title}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{row.message}</Text>
        </Space>
      ),
    },
    {
      title: 'Status',
      dataIndex: 'status',
      width: 110,
      render: (value: string, row: AlertDto) => (
        <Space direction="vertical" size={0}>
          <Tag color={value === 'active' ? 'green' : 'default'}>
            {value === 'active' ? 'Active' : 'Resolved'}
          </Tag>
          {row.resolved_at && (
            <Text type="secondary" style={{ fontSize: 11 }}>
              {new Date(row.resolved_at).toLocaleString()}
            </Text>
          )}
        </Space>
      ),
    },
    {
      title: 'Task',
      dataIndex: 'task_id',
      width: 120,
      render: (taskId: string | null) => taskId
        ? <Link to={`/tasks?task=${encodeURIComponent(taskId)}`}>{taskId.slice(0, 14)}</Link>
        : <Text type="secondary">—</Text>,
    },
    {
      title: 'Service',
      dataIndex: 'service_name',
      width: 220,
      render: (_: string, row: AlertDto) => <Text>{affectedServices(row)}</Text>,
    },
    {
      title: 'Created',
      dataIndex: 'created_at',
      width: 160,
      render: (value: string | null) => value
        ? new Date(value).toLocaleString()
        : <Text type="secondary">—</Text>,
    },
    {
      title: 'Read',
      dataIndex: 'read',
      width: 190,
      render: (_: boolean, row: AlertDto) => row.read
        ? (
          <Space direction="vertical" size={0}>
            <Badge status="success" text="Read" />
            <Text type="secondary" style={{ fontSize: 11 }}>
              {row.read_by_name || row.read_by || 'unknown'}
              {row.read_at ? ` · ${new Date(row.read_at).toLocaleString()}` : ''}
            </Text>
          </Space>
        )
        : <Badge status="processing" text="Unread" />,
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 170,
      render: (_: unknown, row: AlertDto) => (
        <Space size={4}>
          <Button size="small" onClick={() => setDetail(row)}>Details</Button>
          <Button size="small" onClick={() => setRead([row.id], !row.read)}>
            {row.read ? 'Unread' : 'Read'}
          </Button>
        </Space>
      ),
    },
  ], [fetchAlerts])

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 12 }} wrap>
        <Space wrap>
          <Typography.Title level={3} style={{ margin: 0 }}>Alerts</Typography.Title>
          <Badge count={unread} size="small" />
        </Space>
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="Search type, title, message, user, service, task, context"
            style={{ width: 380 }}
            onSearch={(value) => { setOffset(0); setQ(value.trim()) }}
          />
          <Select
            value={status ?? 'all'}
            style={{ width: 130 }}
            onChange={(value: 'active' | 'resolved' | 'all') => {
              setOffset(0)
              setStatus(value === 'all' ? undefined : value)
            }}
            options={[
              { value: 'all', label: 'All statuses' },
              { value: 'active', label: 'Active' },
              { value: 'resolved', label: 'Resolved' },
            ]}
          />
          <Button icon={<ReloadOutlined />} onClick={() => fetchAlerts()}>Refresh</Button>
        </Space>
      </Space>

      <Space style={{ marginBottom: 12 }} wrap>
        <Button disabled={selected.length === 0} onClick={() => setRead(selected, true)}>
          Mark read
        </Button>
        <Button disabled={selected.length === 0} onClick={() => setRead(selected, false)}>
          Mark unread
        </Button>
        {/* R3/CH17 — Read all is always available. The backend marks every
            unread row (active or resolved), independent of the current filter;
            the unread-active badge must not gate it. */}
        <Button onClick={readAll}>Read all</Button>
        {selected.length > 0 && <Text type="secondary">{selected.length} selected</Text>}
      </Space>

      {error && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message="Could not load alerts"
          description={error}
          action={<Button size="small" onClick={() => fetchAlerts()}>Retry</Button>}
        />
      )}

      <Table<AlertDto>
        rowKey="id"
        loading={loading}
        dataSource={rows}
        columns={columns}
        locale={{ emptyText: <Empty description={error ? 'Failed to load' : 'No alerts'} /> }}
        rowSelection={{
          selectedRowKeys: selected,
          onChange: (keys) => setSelected(keys as string[]),
        }}
        pagination={{
          current: Math.floor(offset / limit) + 1,
          pageSize: limit,
          total,
          showSizeChanger: true,
          pageSizeOptions: [25, 50, 100, 200],
          onChange: (page, size) => {
            const next = Math.max(1, size || limit)
            setLimit(next)
            setOffset((page - 1) * next)
          },
        }}
      />

      <Drawer
        title={detail ? `${alertTypeLabel(detail.type)} — ${detail.title}` : 'Alert'}
        placement="right"
        width={640}
        open={!!detail}
        onClose={() => {
          setDetail(null)
          if (alertParam) setSearchParams({})
        }}
      >
        {detail && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Descriptions size="small" column={1} bordered>
              <Descriptions.Item label="Status">
                <Tag color={detail.status === 'active' ? 'green' : 'default'}>
                  {detail.status === 'active' ? 'Active' : 'Resolved'}
                </Tag>
                {detail.resolved_at && (
                  <Text type="secondary"> · {new Date(detail.resolved_at).toLocaleString()}</Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="Severity">{detail.severity}</Descriptions.Item>
              <Descriptions.Item label="Kind">{detail.kind}</Descriptions.Item>
              <Descriptions.Item label="Condition key">{detail.condition_key || '—'}</Descriptions.Item>
              <Descriptions.Item label="Source key">{detail.source_key || '—'}</Descriptions.Item>
              <Descriptions.Item label="Task">{detail.task_id || '—'}</Descriptions.Item>
              <Descriptions.Item label="Service">
                {affectedServices(detail)}
              </Descriptions.Item>
              <Descriptions.Item label="Service id">{detail.service_id || '—'}</Descriptions.Item>
              <Descriptions.Item label="Created">
                {detail.created_at ? new Date(detail.created_at).toLocaleString() : '—'}
              </Descriptions.Item>
              <Descriptions.Item label="Last seen">
                {detail.last_seen_at ? new Date(detail.last_seen_at).toLocaleString() : '—'}
              </Descriptions.Item>
              <Descriptions.Item label="Read">
                {detail.read
                  ? `${detail.read_by_name || detail.read_by || 'unknown'} · ${detail.read_at ? new Date(detail.read_at).toLocaleString() : ''}`
                  : 'Unread'}
              </Descriptions.Item>
            </Descriptions>
            <div>
              <Text strong>Message</Text>
              <Paragraph style={{ marginTop: 4 }}>{detail.message}</Paragraph>
            </div>
            <div>
              <Text strong>Context</Text>
              <pre style={{ marginTop: 4, maxHeight: 320, overflow: 'auto', fontSize: 12 }}>
                {JSON.stringify(detail.context || {}, null, 2)}
              </pre>
            </div>
            <Space>
              <Button onClick={() => setRead([detail.id], !detail.read)}>
                {detail.read ? 'Mark unread' : 'Mark read'}
              </Button>
              <Button onClick={() => { setDetail(null); if (alertParam) setSearchParams({}) }}>
                Close
              </Button>
            </Space>
          </Space>
        )}
      </Drawer>
    </div>
  )
}
