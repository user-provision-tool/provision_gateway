import { useState, useEffect, useCallback } from 'react'
import { Typography, Table, Space, Select, DatePicker, Button, Tag, Input, message } from 'antd'
import { ReloadOutlined, DownloadOutlined, ClearOutlined } from '@ant-design/icons'
import { usePolling } from '../hooks/usePolling'
import client from '../api/client'

const { Title } = Typography
const { RangePicker } = DatePicker

const ACTION_COLORS: Record<string,string> = {
  register:'green', remove:'red', rebuild:'blue', config_edit:'orange',
  admin_create:'purple', deploy:'cyan', clone:'geekblue', password_change:'gold',
}

export default function AuditPage() {
  const [entries, setEntries] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [filters, setFilters] = useState<{action?:string;admin_id?:string;target_user?:string;start?:string;end?:string}>({})

  const fetchAudit = useCallback(async () => {
    setLoading(true)
    try {
      const params: any = { limit: 200 }
      if (filters.action) params.action = filters.action
      if (filters.admin_id) params.admin_id = filters.admin_id
      if (filters.target_user) params.target_user = filters.target_user
      if (filters.start) params.start = filters.start
      if (filters.end) params.end = filters.end
      const { data } = await client.get('/audit', { params })
      setEntries(data.entries || data.audit_logs || [])
    } catch { message.error('Failed to load audit log') }
    finally { setLoading(false) }
  }, [filters])

  useEffect(() => { fetchAudit() }, [fetchAudit])
  usePolling(fetchAudit, 30000)

  const exportCSV = () => {
    const header = 'Timestamp,Admin,Action,Target User,Target Service,Status,Detail\n'
    const rows = entries.map((e:any) =>
      `"${e.created_at||''}","${e.admin_email||e.admin_id||''}","${e.action||''}","${e.target_user||''}","${e.target_service||''}","${e.status||''}","${JSON.stringify(e.detail_json||e.detail||'')}"`
    ).join('\n')
    const blob = new Blob([header+rows], {type:'text/csv'})
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href=url; a.download='audit-log.csv'; a.click()
    URL.revokeObjectURL(url)
  }

  const columns = [
    { title:'Time', dataIndex:'created_at', key:'created_at', width:170,
      render:(v:string)=>v?new Date(v).toLocaleString():'-' },
    { title:'Admin', dataIndex:'admin_email', key:'admin_email', width:200,
      render:(v:string)=>v||'-' },
    { title:'Action', dataIndex:'action', key:'action', width:130,
      render:(a:string)=><Tag color={ACTION_COLORS[a]||'default'}>{a||'-'}</Tag> },
    { title:'Target User', dataIndex:'target_user', key:'target_user', width:130,
      render:(v:string)=>v||'-' },
    { title:'Target Service', dataIndex:'target_service', key:'target_service', width:150,
      render:(v:string)=>v||'-' },
    { title:'Status', dataIndex:'status', key:'status', width:90,
      render:(s:string)=>s==='success'?<Tag color="green">✓</Tag>:s==='failure'?<Tag color="red">✗</Tag>:<Tag>{s||'-'}</Tag> },
    { title:'Detail', dataIndex:'detail_json', key:'detail', ellipsis:true,
      render:(d:any)=><small>{typeof d==='string'?d:JSON.stringify(d||'')}</small> },
  ]

  return (
    <div>
      <div style={{display:'flex',justifyContent:'space-between',marginBottom:16,flexWrap:'wrap',gap:8}}>
        <Title level={3} style={{margin:0}}>Audit Log</Title>
        <Space wrap>
          <Button icon={<ReloadOutlined/>} onClick={fetchAudit} loading={loading}>Refresh</Button>
          <Button icon={<DownloadOutlined/>} onClick={exportCSV} disabled={entries.length===0}>CSV</Button>
        </Space>
      </div>

      <Space wrap style={{marginBottom:16}}>
        <Select placeholder="Action" allowClear style={{width:150}} onChange={(v)=>setFilters(f=>({...f,action:v}))}>
          {['register','remove','rebuild','deploy','clone','config_edit','admin_create','password_change'].map(a=><Select.Option key={a} value={a}>{a}</Select.Option>)}
        </Select>
        <Input placeholder="Target User" allowClear style={{width:150}} onChange={e=>setFilters(f=>({...f,target_user:e.target.value||undefined}))}/>
        <RangePicker onChange={(_,ds)=>{setFilters(f=>({...f,start:ds[0]||undefined,end:ds[1]||undefined}))}}/>
        <Button icon={<ClearOutlined/>} onClick={()=>setFilters({})}>Clear</Button>
      </Space>

      <Table
        dataSource={entries}
        columns={columns}
        rowKey={(r:any)=>r.id||Math.random()}
        loading={loading}
        pagination={{pageSize:25,showSizeChanger:true,showTotal:(t)=>`${t} entries`}}
        size="small"
        scroll={{x:1000}}
      />
    </div>
  )
}
