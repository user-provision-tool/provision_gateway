import { useState, useEffect, useCallback } from 'react'
import { Typography, Table, Space, Button, Tag, Progress, Drawer, message, Popconfirm } from 'antd'
import { ReloadOutlined, EyeOutlined, DeleteOutlined, PauseCircleOutlined } from '@ant-design/icons'
import { usePolling } from '../hooks/usePolling'
import { useSSE } from '../hooks/useSSE'
import client from '../api/client'

const { Title } = Typography

const STATUS_COLORS: Record<string,string> = {
  pending:'default', running:'processing', completed:'success', succeeded:'success',
  failed:'error', cancelled:'warning',
}

export default function TasksPage() {
  const [tasks, setTasks] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [logTaskId, setLogTaskId] = useState<string | null>(null)
  const [logDrawerOpen, setLogDrawerOpen] = useState(false)

  const fetchTasks = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await client.get('/tasks')
      const list = data.tasks || data || []
      setTasks(Array.isArray(list) ? list : [])
    } catch { /* silent */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { fetchTasks() }, [fetchTasks])
  usePolling(fetchTasks, 5000)

  const cancelTask = async (id: string) => {
    try { await client.delete(`/tasks/${id}`); message.success('Cancelled'); fetchTasks() }
    catch { message.error('Failed to cancel') }
  }

  // SSE log streaming — uses /api prefix for gateway proxy; token via query param for EventSource
  const sseUrl = logTaskId ? `/api/tasks/${logTaskId}/log` : null
  const { lines: logLines, isConnected: logConnected, clearLines: clearLog } = useSSE(sseUrl)

  const openLogs = (taskId: string) => {
    setLogTaskId(taskId)
    setLogDrawerOpen(true)
    clearLog()
  }

  const closeLogs = () => {
    setLogDrawerOpen(false)
    setLogTaskId(null)
    clearLog()
  }

  const formatElapsed = (createdAt: number) => {
    if (!createdAt) return '-'
    const elapsed = Math.floor((Date.now() / 1000) - createdAt)
    if (elapsed < 60) return `${elapsed}s`
    if (elapsed < 3600) return `${Math.floor(elapsed/60)}m ${elapsed%60}s`
    const h = Math.floor(elapsed/3600)
    const m = Math.floor((elapsed%3600)/60)
    return `${h}h ${m}m`
  }

  const columns = [
    { title:'ID', dataIndex:'task_id', key:'task_id', width:100, ellipsis:true,
      render:(v:string)=>v?v.substring(0,8)+'...':'-' },
    { title:'Type', dataIndex:'type', key:'type', width:100,
      render:(t:string)=><Tag>{t||'task'}</Tag> },
    { title:'Target', key:'target', width:150,
      render:(_:any,r:any)=>{
        if (r.result && typeof r.result === 'object') {
          return <span>{r.result.user_name||''}{r.result.service_name?`/${r.result.service_name}`:''}{r.result.label?`/${r.result.label}`:''}</span>
        }
        return <span>{r.type||'task'}</span>
      }},
    { title:'Status', dataIndex:'status', key:'status', width:110,
      render:(s:string)=><Tag color={STATUS_COLORS[s]||'default'}>{s||'unknown'}</Tag> },
    { title:'Updated', dataIndex:'updated_at', key:'updated_at', width:160,
      render:(v:number)=>v?new Date(v*1000).toLocaleString():'-' },
    { title:'Elapsed', key:'elapsed', width:100,
      render:(_:any,r:any)=>formatElapsed(r.created_at) },
    { title:'Created', dataIndex:'created_at', key:'created_at', width:160,
      render:(v:number)=>v?new Date(v*1000).toLocaleString():'-' },
    { title:'Actions', key:'actions', width:120,
      render:(_:any,r:any)=><Space>
        <Button size="small" icon={<EyeOutlined/>} onClick={()=>openLogs(r.task_id)}>Logs</Button>
        {(r.status==='pending'||r.status==='running')&&<Popconfirm title="Cancel?" onConfirm={()=>cancelTask(r.task_id)}><Button size="small" danger icon={<PauseCircleOutlined/>}/></Popconfirm>}
        <Popconfirm title="Delete?" onConfirm={()=>client.delete(`/tasks/${r.task_id}`).then(()=>fetchTasks())}><Button size="small" icon={<DeleteOutlined/>}/></Popconfirm>
      </Space> },
  ]

  return (
    <div>
      <div style={{display:'flex',justifyContent:'space-between',marginBottom:16,flexWrap:'wrap',gap:8}}>
        <Title level={3} style={{margin:0}}>Tasks</Title>
        <Button icon={<ReloadOutlined/>} onClick={fetchTasks}>Refresh</Button>
      </div>

      <Table
        dataSource={tasks}
        columns={columns}
        rowKey={(r:any)=>r.task_id||Math.random()}
        loading={loading}
        pagination={{pageSize:20,showSizeChanger:true,showTotal:(t)=>`${t} tasks`}}
        size="small"
        scroll={{x:900}}
        locale={{emptyText:'No tasks in queue.'}}
      />

      {/* Log drawer */}
      <Drawer
        title={logConnected ? <Tag color="green">Live</Tag> : <Tag color="default">Disconnected</Tag>}
        placement="right"
        width={700}
        open={logDrawerOpen}
        onClose={closeLogs}
      >
        <div style={{
          background:'#1e1e1e', color:'#d4d4d4', fontFamily:'monospace', fontSize:12,
          padding:12, borderRadius:4, maxHeight:'calc(100vh - 160px)', overflow:'auto',
          whiteSpace:'pre-wrap', wordBreak:'break-all'
        }}>
          {logLines.length===0 ? <span style={{color:'#888'}}>Waiting for logs...</span> :
            logLines.map((line,i)=><div key={i}>{line}</div>)}
        </div>
      </Drawer>
    </div>
  )
}
