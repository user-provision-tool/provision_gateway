import { useState, useEffect, useCallback } from 'react'
import { Typography, Card, Row, Col, Statistic, Tag, Space, Table, Progress, Button, message } from 'antd'
import { GlobalOutlined, CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined, ReloadOutlined, SyncOutlined } from '@ant-design/icons'
import { useAuth } from '../hooks/useAuth'
import { usePolling } from '../hooks/usePolling'
import client from '../api/client'

const { Title, Text } = Typography

export default function DashboardPage() {
  const { admin } = useAuth()
  const [sysStatus, setSysStatus] = useState<any>(null)
  const [proxyStatus, setProxyStatus] = useState<any>(null)
  const [stats, setStats] = useState<any>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [userServices, setUserServices] = useState<Record<string,any>>({})

  const fetchAll = useCallback(async () => {
    // Use individual try/catch so one failure doesn't block others
    try {
      const statusRes = await client.get('/system/status')
      setSysStatus(statusRes.data)
    } catch { /* system/status may fail */ }
    
    try {
      const proxyRes = await client.get('/system/proxy')
      setProxyStatus(proxyRes.data)
    } catch { /* proxy not configured */ }
    
    try {
      const statsRes = await client.get('/system/stats')
      setStats(statsRes.data)
    } catch { /* stats may fail */ }
    
    try {
      const usersRes = await client.get('/users')
      const users = usersRes.data.users || usersRes.data.user_status || []
      const summary: Record<string,any> = {}
      for (const u of users) {
        const un = u.user_name || 'unknown'
        const services = [...(u.healthy_services||[]), ...(u.unhealthy_services||[]), ...(u.missing_services||[])]
        summary[un] = {
          services: services.length,
          healthy: (u.healthy_services||[]).length,
          unhealthy: (u.unhealthy_services||[]).length + (u.missing_services||[]).length,
        }
      }
      setUserServices(summary)
    } catch { /* users may fail */ }
    
    setRefreshing(true)
    setTimeout(() => setRefreshing(false), 500)
  }, [])

  useEffect(() => { fetchAll() }, [])
  usePolling(fetchAll, 10000)

  const compColumns = [
    { title: 'Component', dataIndex: 'name', key: 'name', render: (n:string)=><Text strong>{n}</Text> },
    { title: 'Status', dataIndex: 'status', key: 'status',
      render: (s:string) => s==='running' ? <Tag color="green">Running</Tag> : <Tag color="red">{s}</Tag> },
  ]

  const containers = stats?.containers || []
  const cpuPct = sysStatus?.docker_host?.cpu_percent ?? null
  const ramPct = sysStatus?.docker_host?.mem_percent ?? null
  const diskPct = sysStatus?.docker_host?.disk_percent ?? null

  return (
    <div>
      <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:16,flexWrap:'wrap',gap:8}}>
        <Title level={3} style={{margin:0}}>Dashboard</Title>
        <Space>
          {refreshing && <Tag icon={<SyncOutlined spin/>} color="processing">Live</Tag>}
          <Button icon={<ReloadOutlined/>} size="small" onClick={fetchAll}>Refresh</Button>
        </Space>
      </div>

      {/* Stat cards */}
      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} md={6}><Card><Statistic title="Services" value={sysStatus?.services_count ?? 0}/></Card></Col>
        <Col xs={24} sm={12} md={6}><Card><Statistic title="Users" value={sysStatus?.users_count ?? 0}/></Card></Col>
        <Col xs={24} sm={12} md={6}><Card><Statistic title="Running Tasks" value={sysStatus?.tasks_running ?? 0}/></Card></Col>
        <Col xs={24} sm={12} md={6}><Card><Statistic title="Containers" value={sysStatus?.docker_host?.containers_running||0} suffix={`/ ${sysStatus?.docker_host?.containers_total||0}`}/></Card></Col>
      </Row>

      {/* CPU / RAM / Disk gauges */}
      <Row gutter={[16, 16]} style={{marginTop:16}}>
        <Col xs={24} sm={8}>
          <Card size="small" title="CPU">
            {cpuPct !== null ? <Progress type="dashboard" percent={Math.round(cpuPct)} size={120} status={cpuPct>80?'exception':'normal'}/> : <Text type="secondary">Loading...</Text>}
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small" title="RAM">
            {ramPct !== null ? <Progress type="dashboard" percent={Math.round(ramPct)} size={120} status={ramPct>80?'exception':'normal'}/> : <Text type="secondary">Loading...</Text>}
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small" title="Disk">
            {diskPct !== null ? <Progress type="dashboard" percent={Math.round(diskPct)} size={120} status={diskPct>80?'exception':'normal'}/> : <Text type="secondary">Loading...</Text>}
          </Card>
        </Col>
      </Row>

      {/* System Components + Proxy */}
      <Row gutter={[16, 16]} style={{marginTop:16}}>
        <Col xs={24} md={12}>
          <Card title="System Components" size="small" extra={
            <Button size="small" onClick={()=>client.post('/system/reconcile').then(()=>message.info('Reconciliation triggered')).catch(()=>message.error('Reconcile failed'))}>Reconcile</Button>
          }>
            {sysStatus?.components ? (
              <Table dataSource={Object.entries(sysStatus.components).map(([k,v]:[string,any])=>({name:k,status:v.status}))} columns={compColumns} rowKey="name" pagination={false} showHeader={false} size="small"/>
            ) : <Text type="secondary">Loading...</Text>}
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title={<Space><GlobalOutlined/>Global Proxy</Space>} size="small">
            {proxyStatus ? (
              proxyStatus.has_active ? (
                <Tag icon={<CheckCircleOutlined/>} color="success">Active — {proxyStatus.active?.url}</Tag>
              ) : (
                <Tag color="default">No active proxy</Tag>
              )
            ) : <Text type="secondary">Loading...</Text>}
          </Card>
        </Col>
      </Row>

      {/* User summary cards */}
      {Object.keys(userServices).length > 0 && (
        <Card title="Users" size="small" style={{marginTop:16}}>
          <Row gutter={[12,12]}>
            {Object.entries(userServices as Record<string,any>).map(([user, info]:[string,any]) => (
              <Col xs={24} sm={12} md={8} lg={6} key={user}>
                <Card size="small" hoverable>
                  <Statistic title={user} value={info.services??0} suffix="services"/>
                  <Space style={{marginTop:4}}>
                    <Tag color="green">{info.healthy??0} healthy</Tag>
                    <Tag color="orange">{info.unhealthy??0} unhealthy</Tag>
                  </Space>
                </Card>
              </Col>
            ))}
          </Row>
        </Card>
      )}

      <Card style={{marginTop:16}}>
        <Title level={5}>Welcome, {admin?.email || 'Admin'}!</Title>
        <p>Provision Gateway is running. Use the sidebar to manage services, users, and tasks.</p>
      </Card>
    </div>
  )
}
