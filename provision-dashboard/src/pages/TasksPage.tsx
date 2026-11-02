import { useState, useEffect, useCallback, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Typography, Table, Space, Button, Tag, Drawer, message, Popconfirm } from 'antd'
import { ReloadOutlined, EyeOutlined, PauseCircleOutlined } from '@ant-design/icons'
import { usePolling } from '../hooks/usePolling'
import { useSSE } from '../hooks/useSSE'
import client from '../api/client'

const { Title, Text } = Typography

const epochSeconds = (v: any): number | undefined => {
  if (v == null) return undefined
  const s = String(v).trim()
  if (!s) return undefined
  const hasZone = /[Zz]$|[+-]\d\d:?\d\d$/.test(s)
  const ms = Date.parse(hasZone ? s : `${s.replace(' ', 'T')}Z`)
  return Number.isNaN(ms) ? undefined : ms / 1000
}

const STATUS_COLORS: Record<string, string> = {
  queued: 'default', running: 'processing', paused: 'warning', awaiting_input: 'warning',
  completed: 'success', failed: 'error', cancelled: 'warning', unknown: 'default',
}

type Row = {
  key: string; source: 'gateway' | 'api'; id: string; type: string
  target: string; status: string; progress?: string
  created_at?: number; updated_at?: number; raw: any
}

/** One list, one read model (R4.5): the gateway task view already unions both
 *  families, so this page never merges two endpoints itself. */
export default function TasksPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [tasks, setTasks] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [logTaskId, setLogTaskId] = useState<string | null>(null)
  const [logDrawerOpen, setLogDrawerOpen] = useState(false)
  const firstLoadRef = useRef(true)

  const fetchTasks = useCallback(async () => {
    if (firstLoadRef.current) setLoading(true)
    try {
      const { data } = await client.get('/tasks')
      setTasks(data.tasks || [])
    } catch { /* silent */ }
    finally {
      if (firstLoadRef.current) { firstLoadRef.current = false; setLoading(false) }
    }
  }, [])

  useEffect(() => { fetchTasks() }, [fetchTasks])
  usePolling(fetchTasks, 5000)

  const cancelTask = async (id: string) => {
    try { await client.delete(`/tasks/${id}`); message.success('Cancelled'); fetchTasks() }
    catch (err: any) { message.error(err.response?.data?.detail || 'Failed to cancel') }
  }

  const sseUrl = logTaskId ? `/api/tasks/${logTaskId}/log` : null
  const { lines: logLines, isConnected: logConnected, clearLines: clearLog } = useSSE(sseUrl)

  const openLogs = useCallback((taskId: string) => {
    setLogTaskId(taskId); setLogDrawerOpen(true); clearLog()
  }, [clearLog])

  const jobParam = searchParams.get('job') || ''
  const taskParam = searchParams.get('task') || ''
  useEffect(() => {
    if (jobParam) openLogs(jobParam)
    else if (taskParam) openLogs(taskParam)
  }, [jobParam, taskParam, openLogs])

  const closeLogs = () => {
    setLogDrawerOpen(false); setLogTaskId(null); clearLog()
    if (jobParam || taskParam) setSearchParams({})
  }

  const formatElapsed = (createdAt: number) => {
    if (!createdAt) return '-'
    const elapsed = Math.floor((Date.now() / 1000) - createdAt)
    if (elapsed < 60) return `${elapsed}s`
    if (elapsed < 3600) return `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`
    return `${Math.floor(elapsed / 3600)}h ${Math.floor((elapsed % 3600) / 60)}m`
  }

  const rows: Row[] = tasks.map((t: any): Row => ({
    key: t.task_id,
    source: String(t.task_id).startsWith('gt-') ? 'gateway' : 'api',
    id: t.task_id,
    type: t.operation || t.kind || 'task',
    target: t.target_display || t.service_name || '-',
    status: t.status,
    progress: t.progress,
    created_at: epochSeconds(t.created_at),
    updated_at: epochSeconds(t.updated_at),
    raw: t,
  })).sort((a, b) => (b.created_at || 0) - (a.created_at || 0))

  const columns = [
    { title: 'Source', dataIndex: 'source', key: 'source', width: 90,
      render: (s: string) => <Tag color={s === 'gateway' ? 'geekblue' : 'default'}>{s}</Tag> },
    { title: 'ID', dataIndex: 'id', key: 'id', width: 150, ellipsis: true },
    { title: 'Type', dataIndex: 'type', key: 'type', width: 130,
      render: (t: string) => <Tag>{t || 'task'}</Tag> },
    { title: 'Target', dataIndex: 'target', key: 'target', width: 220, ellipsis: true },
    { title: 'Status', dataIndex: 'status', key: 'status', width: 110,
      render: (s: string) => <Tag color={STATUS_COLORS[s] || 'default'}>{s || 'unknown'}</Tag> },
    { title: 'Progress', dataIndex: 'progress', key: 'progress', ellipsis: true,
      render: (p: string) => <Text type="secondary" style={{ fontSize: 12 }}>{p || '-'}</Text> },
    { title: 'Procedure', key: 'procedure', width: 120, ellipsis: true,
      render: (_: any, r: Row) => <Text code style={{ fontSize: 11 }}>{String(r.raw.procedure_id || '').slice(0, 8)}</Text> },
    { title: 'Updated', dataIndex: 'updated_at', key: 'updated_at', width: 160,
      render: (v: number) => v ? new Date(v * 1000).toLocaleString() : '-' },
    { title: 'Elapsed', key: 'elapsed', width: 100,
      render: (_: any, r: Row) => formatElapsed(r.created_at || 0) },
    { title: 'Actions', key: 'actions', width: 160,
      render: (_: any, r: Row) => (
        <Space>
          <Button size="small" icon={<EyeOutlined />} onClick={() => openLogs(r.id)}>Logs</Button>
          {['queued', 'running'].includes(r.status) && (
            <Popconfirm title="Cancel?" onConfirm={() => cancelTask(r.id)}>
              <Button size="small" danger icon={<PauseCircleOutlined />} />
            </Popconfirm>
          )}
        </Space>
      ) },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16, flexWrap: 'wrap', gap: 8 }}>
        <Title level={3} style={{ margin: 0 }}>Tasks</Title>
        <Button icon={<ReloadOutlined />} onClick={fetchTasks}>Refresh</Button>
      </div>
      <Title level={5} style={{ marginTop: 0 }}>All tasks <Text type="secondary" style={{ fontSize: 12, fontWeight: 'normal' }}>
        — one read model: gateway generation (<Text code>gt-</Text>) and core deploy (<Text code>api-</Text>) tasks</Text></Title>
      <Table dataSource={rows} columns={columns} rowKey={(r: Row) => r.key}
        loading={loading} pagination={{ pageSize: 20, showSizeChanger: true, showTotal: (t) => `${t} records` }}
        size="small" scroll={{ x: 1200 }} locale={{ emptyText: 'Nothing running.' }} />

      <Drawer title={logConnected ? <Tag color="green">Live</Tag> : <Tag color="default">Disconnected</Tag>}
        placement="right" width={700} open={logDrawerOpen} onClose={closeLogs}>
        <div style={{
          background: '#1e1e1e', color: '#d4d4d4', fontFamily: 'monospace', fontSize: 12,
          padding: 12, borderRadius: 4, maxHeight: 'calc(100vh - 200px)', overflow: 'auto',
          whiteSpace: 'pre-wrap', wordBreak: 'break-all',
        }}>
          {logLines.length === 0 ? <span style={{ color: '#888' }}>Waiting for logs...</span> :
            logLines.map((line, i) => <div key={i}>{line}</div>)}
        </div>
      </Drawer>
    </div>
  )
}
