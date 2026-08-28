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
  const [subnetPool, setSubnetPool] = useState<any>(null)
  const [refreshing, setRefreshing] = useState(false)

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
      const subnetRes = await client.get('/system/subnet-pool')
      setSubnetPool(subnetRes.data)
    } catch { /* subnet management not configured */ }

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

  const cpuPct = sysStatus?.docker_host?.cpu_percent ?? null
  const ramPct = sysStatus?.docker_host?.mem_percent ?? null
  const diskPct = sysStatus?.docker_host?.disk_percent ?? null

  // ACL indicator: enabled only when BOTH the gateway and the edge are on
  // (a gateway-on/edge-off mismatch was the 2026-08-28 outage).
  const aclTag = (() => {
    if (!sysStatus?.acl) return null
    const a = sysStatus.acl
    if (a.edge === null) return <Tag color="default">ACL: unknown</Tag>
    if (a.enabled) return <Tag icon={<CheckCircleOutlined/>} color="green">ACL: Enabled</Tag>
    if (!a.consistent) return <Tag icon={<CloseCircleOutlined/>} color="orange">ACL: Mismatch</Tag>
    return <Tag icon={<CloseCircleOutlined/>} color="red">ACL: Disabled</Tag>
  })()

  // Registry-based stats (from provision-api, not docker ps)
  const cStats = sysStatus?.container_stats || {}
  const sStats = sysStatus?.service_stats || {}
  const svcHealthy = sStats.healthy ?? 0
  const svcUnhealthy = sStats.unhealthy ?? 0
  const svcExpected = sStats.expected ?? 0

  return (
    <div>
      <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:16,flexWrap:'wrap',gap:8}}>
        <Title level={3} style={{margin:0}}>Dashboard</Title>
        <Space>
          {aclTag}
          {refreshing && <Tag icon={<SyncOutlined spin/>} color="processing">Live</Tag>}
          <Button icon={<ReloadOutlined/>} size="small" onClick={fetchAll}>Refresh</Button>
        </Space>
      </div>

      {/* Stat cards — consistent height, all stats always shown */}
      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} md={6}>
          <Card style={{height:110}}>
            <Statistic title="Services" value={svcExpected}
              suffix={svcExpected > 0 ? <span style={{fontSize:13,display:'block',marginTop:4}}>
                <Tag color="green">{svcHealthy} healthy</Tag>
                {svcUnhealthy > 0 && <Tag color="orange">{svcUnhealthy} unhealthy</Tag>}
              </span> : <span style={{fontSize:12,color:'#999',display:'block',marginTop:4}}>No services yet</span>}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card style={{height:110}}><Statistic title="Users" value={sysStatus?.users_count ?? 0}/></Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card style={{height:110}}><Statistic title="Running Tasks" value={sysStatus?.tasks_running ?? 0}/></Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card style={{height:110}}>
            <Statistic title="Containers" value={cStats.total_expected ?? 0}
              suffix={<span style={{fontSize:12,display:'block',marginTop:4}}>
                <Tag color="green">{(cStats.healthy_running ?? 0)} up</Tag>
                {(cStats.unhealthy_running ?? 0) > 0 && <Tag color="orange">{(cStats.unhealthy_running??0)} unhlth</Tag>}
                {(cStats.restarting ?? 0) > 0 && <Tag color="purple">{(cStats.restarting??0)} rst</Tag>}
                {(cStats.down ?? 0) > 0 && <Tag color="red">{(cStats.down??0)} down</Tag>}
                {(cStats.missing ?? 0) > 0 && <Tag color="default">{(cStats.missing??0)} miss</Tag>}
              </span>}
            />
          </Card>
        </Col>
      </Row>

      {/* CPU / RAM / Disk gauges */}
      <Row gutter={[16, 16]} style={{marginTop:16}}>
        <Col xs={24} sm={8}>
          <Card size="small" title="CPU">
            {cpuPct !== null ? <Progress type="dashboard" percent={Math.round(cpuPct)} size={120} status="normal" strokeColor={cpuPct>80?'#ff4d4f':undefined}/> : <Text type="secondary">Loading...</Text>}
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small" title="RAM">
            {ramPct !== null ? <Progress type="dashboard" percent={Math.round(ramPct)} size={120} status="normal" strokeColor={ramPct>80?'#ff4d4f':undefined}/> : <Text type="secondary">Loading...</Text>}
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small" title="Disk">
            {diskPct !== null ? <Progress type="dashboard" percent={Math.round(diskPct)} size={120} status="normal" strokeColor={diskPct>80?'#ff4d4f':undefined}/> : <Text type="secondary">Loading...</Text>}
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

      {/* Subnet Pool */}
      {subnetPool && (
        <Row gutter={[16, 16]} style={{marginTop:16}}>
          <Col span={24}>
            <Card title={<Space><GlobalOutlined />Subnet Pool</Space>} size="small">
              {subnetPool.enabled ? (
                <Row gutter={[16, 16]}>
                  {subnetPool.pools.map((pool: any, i: number) => {
                    const color = pool.used_pct > 90 ? '#ff4d4f' : pool.used_pct > 70 ? '#faad14' : '#52c41a'
                    return (
                      <Col xs={24} sm={12} md={8} key={i}>
                        <Card size="small" bodyStyle={{padding:'12px 16px'}}>
                          <div style={{display:'flex', justifyContent:'space-between', alignItems:'center'}}>
                            <Text strong>{pool.cidr}</Text>
                            <Tag color={pool.used_pct > 90 ? 'red' : pool.used_pct > 70 ? 'orange' : 'green'}>
                              {pool.used_pct}%
                            </Tag>
                          </div>
                          <Progress
                            percent={pool.used_pct}
                            strokeColor={color}
                            showInfo={false}
                            style={{marginTop:8}}
                          />
                          <Text type="secondary" style={{fontSize:11}}>
                            {pool.used_slots} / {pool.total_slots} slots used
                          </Text>
                        </Card>
                      </Col>
                    )
                  })}
                </Row>
              ) : (
                <Text type="secondary">Subnet management is disabled. Set SUBNET_POOLS to enable per-service subnet allocation.</Text>
              )}
            </Card>
          </Col>
        </Row>
      )}

      {/* Welcome */}
      <Card style={{marginTop:16}}>
        <Title level={5}>Welcome, {admin?.email || 'Admin'}!</Title>
        <p>Provision Gateway is running. Use the sidebar to manage services, users, and tasks.</p>
      </Card>
    </div>
  )
}
