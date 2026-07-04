import { useState, useEffect } from 'react'
import { Typography, Card, Table, Tag, Space, Button, Empty, Spin, message } from 'antd'
import { LinkOutlined, ReloadOutlined } from '@ant-design/icons'
import client from '../api/client'

const { Title } = Typography

interface ServiceInstance {
  user_name: string
  service_name: string
  label: string
  healthy_containers?: Record<string,string>
  unhealthy_containers?: Record<string,string>
  missing_containers?: Record<string,string>
  compose_file_path?: string
}

export default function UsersPage() {
  const [services, setServices] = useState<ServiceInstance[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => { loadServices() }, [])

  const loadServices = async () => {
    setLoading(true)
    try {
      const { data } = await client.get('/users')
      const users = data.users || data.user_status || []
      // Flatten: each user may have multiple services
      const all: ServiceInstance[] = []
      for (const u of users) {
        for (const s of (u.healthy_services||[])) all.push({...s, user_name: u.user_name})
        for (const s of (u.unhealthy_services||[])) all.push({...s, user_name: u.user_name})
        for (const s of (u.missing_services||[])) all.push({...s, user_name: u.user_name})
      }
      setServices(all)
    } catch (err: any) {
      message.error('Failed to load services')
    } finally { setLoading(false) }
  }

  const columns = [
    { title: 'User', dataIndex: 'user_name', key: 'user' },
    { title: 'Service', dataIndex: 'service_name', key: 'service' },
    { title: 'Label', dataIndex: 'label', key: 'label' },
    { title: 'Status', key: 'status',
      render: (_:any, r:ServiceInstance) => {
        const healthy = Object.keys(r.healthy_containers||{}).length
        const unhealthy = Object.keys(r.unhealthy_containers||{}).length
        const missing = Object.keys(r.missing_containers||{}).length
        if (!healthy && !unhealthy && !missing) return <Tag>unknown</Tag>
        if (!unhealthy && !missing) return <Tag color="green">Running ({healthy})</Tag>
        return <Tag color="orange">{healthy} up, {unhealthy+missing} down</Tag>
      }
    },
  ]

  return (
    <div>
      <div style={{display:'flex',justifyContent:'space-between',marginBottom:16}}>
        <Title level={3} style={{margin:0}}>Services</Title>
        <Button icon={<ReloadOutlined/>} onClick={loadServices}>Refresh</Button>
      </div>
      <Card>
        {loading ? <Spin/> : services.length===0 ? <Empty description="No services deployed yet. Add a source project and deploy it to a user." /> :
        <Table dataSource={services} columns={columns} rowKey={(r:any)=>`${r.user_name}-${r.service_name}-${r.label}`} pagination={false}/>}
      </Card>
    </div>
  )
}
