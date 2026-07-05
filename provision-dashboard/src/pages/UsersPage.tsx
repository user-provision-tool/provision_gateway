import { useState, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { Typography, Card, Tag, Space, Button, Empty, Spin, message, Input, Collapse, Badge, Tooltip, Popconfirm, Modal } from 'antd'
import { RocketOutlined, ReloadOutlined, EyeOutlined, EyeInvisibleOutlined, SearchOutlined, UpOutlined, DownOutlined, DeleteOutlined, CopyOutlined, LinkOutlined, KeyOutlined, SwapOutlined, UnorderedListOutlined } from '@ant-design/icons'
import { useAuth } from '../hooks/useAuth'
import client from '../api/client'
import DeployForm from '../components/services/DeployForm'

const { Title, Text } = Typography

interface ServiceInstance {
  user_name: string; service_name: string; label: string;
  healthy_containers?: Record<string,string>
  unhealthy_containers?: Record<string,string>
  missing_containers?: Record<string,string>
  compose_template_path?: string; nginx_conf_template_path?: string
  has_auth?: boolean; url?: string
}

export default function UsersPage() {
  const { admin } = useAuth()
  const navigate = useNavigate()
  const isAdmin = admin?.role === 'admin'
  const [services, setServices] = useState<ServiceInstance[]>([])
  const [loading, setLoading] = useState(true)
  const [deployOpen, setDeployOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [visiblePwds, setVisiblePwds] = useState<Record<string,boolean>>({})
  const [activeTasks, setActiveTasks] = useState<Record<string,string>>({}) // key->taskId

  // Password change modal
  const [pwdModalOpen, setPwdModalOpen] = useState(false)
  const [pwdTarget, setPwdTarget] = useState<{user:string;service:string;label:string}|null>(null)
  const [newPassword, setNewPassword] = useState('')
  const [pwdLoading, setPwdLoading] = useState(false)

  // Clone modal
  const [cloneOpen, setCloneOpen] = useState(false)
  const [cloneSource, setCloneSource] = useState<string | null>(null)
  const [cloneTarget, setCloneTarget] = useState('')
  const [cloneLoading, setCloneLoading] = useState(false)

  useEffect(() => { loadServices() }, [])

  const loadServices = async () => {
    setLoading(true)
    try {
      const { data } = await client.get('/users')
      const users = data.users || data.user_status || []
      const all: ServiceInstance[] = []
      for (const u of users) {
        for (const s of (u.healthy_services||[])) all.push({...s, user_name: u.user_name})
        for (const s of (u.unhealthy_services||[])) all.push({...s, user_name: u.user_name})
        for (const s of (u.missing_services||[])) all.push({...s, user_name: u.user_name})
      }
      setServices(all)
    } catch (err: any) { message.error('Failed to load services') }
    finally { setLoading(false) }
  }

  const grouped = useMemo(() => {
    const map: Record<string, ServiceInstance[]> = {}
    for (const s of services) {
      const un = s.user_name || 'unknown'
      if (!map[un]) map[un] = []
      map[un].push(s)
    }
    if (!search) return map
    const filtered: Record<string, ServiceInstance[]> = {}
    const q = search.toLowerCase()
    for (const [user, svcs] of Object.entries(map)) {
      const match = svcs.filter(s =>
        user.toLowerCase().includes(q) || s.service_name.toLowerCase().includes(q)
      )
      if (match.length > 0) filtered[user] = match
    }
    return filtered
  }, [services, search])

  const getBadge = (s: ServiceInstance) => {
    const h = Object.keys(s.healthy_containers||{}).length
    const uh = Object.keys(s.unhealthy_containers||{}).length
    const m = Object.keys(s.missing_containers||{}).length
    if (!h&&!uh&&!m) return <Badge status="default" text="unknown"/>
    if (!uh&&!m) return <Badge status="success" text={`Running (${h})`}/>
    return <Badge status="warning" text={`${h} up, ${uh+m} down`}/>
  }

  const openPwdChange = (user:string, service:string, label:string) => {
    setPwdTarget({user,service,label})
    setNewPassword('')
    setPwdModalOpen(true)
  }

  const handlePwdChange = async () => {
    if (!pwdTarget || !newPassword) { message.warning('Enter a new password'); return }
    setPwdLoading(true)
    try {
      await client.put(`/users/${pwdTarget.user}/${pwdTarget.service}/${pwdTarget.label}/password`, { passwd: newPassword })
      message.success('Password changed')
      setPwdModalOpen(false)
    } catch (err: any) { message.error(err.response?.data?.detail || 'Failed') }
    finally { setPwdLoading(false) }
  }

  const handleCloneAll = async () => {
    if (!cloneSource || !cloneTarget) { message.warning('Enter target user'); return }
    setCloneLoading(true)
    try {
      await client.post('/users/clone', { source_user: cloneSource, target_user: cloneTarget })
      message.success(`Cloning ${cloneSource} → ${cloneTarget}`)
      setCloneOpen(false)
      loadServices()
    } catch (err: any) { message.error(err.response?.data?.detail || 'Failed') }
    finally { setCloneLoading(false) }
  }

  const testCurl = async (svc: ServiceInstance) => {
    try {
      const r = await client.post(`/users/${svc.user_name}/${svc.service_name}/${svc.label}/test-curl`, { include_auth: true })
      message.info({ content: <div><Text strong>HTTP {r.data.http_code||'?'}</Text><pre style={{fontSize:11,maxHeight:200,overflow:'auto'}}>{r.data.body?.substring(0,500)||r.data.headers||''}</pre></div>, duration: 8 })
    } catch { message.error('Test failed') }
  }

  // Highlight matching text
  const highlight = (text: string, query: string) => {
    if (!query) return <span>{text}</span>
    const idx = text.toLowerCase().indexOf(query.toLowerCase())
    if (idx === -1) return <span>{text}</span>
    return <span>{text.substring(0,idx)}<mark style={{background:'#ffd666',padding:'0 2px'}}>{text.substring(idx,idx+query.length)}</mark>{text.substring(idx+query.length)}</span>
  }

  return (
    <div>
      <div style={{display:'flex',justifyContent:'space-between',marginBottom:16,flexWrap:'wrap',gap:8}}>
        <Title level={3} style={{margin:0}}>Services</Title>
        <Space>
          <Input prefix={<SearchOutlined/>} placeholder="Filter..." value={search} onChange={e=>setSearch(e.target.value)} allowClear style={{width:240}}/>
          {isAdmin && <Button type="primary" icon={<RocketOutlined/>} onClick={()=>setDeployOpen(true)}>Deploy</Button>}
          <Button icon={<ReloadOutlined/>} onClick={loadServices}>Refresh</Button>
        </Space>
      </div>

      {loading ? <Spin/> : Object.keys(grouped).length===0 ? (
        <Card><Empty description={search?"No matches":"No services deployed"}/></Card>
      ) : (
        Object.entries(grouped).map(([userName, userSvcs], idx) => {
          const allHealthy = userSvcs.every(s => Object.keys(s.unhealthy_containers||{}).length===0 && Object.keys(s.missing_containers||{}).length===0)
          return (
          <div key={userName} style={{marginBottom: idx<Object.keys(grouped).length-1?32:0, paddingBottom: idx<Object.keys(grouped).length-1?16:0, borderBottom: idx<Object.keys(grouped).length-1?'1px solid #f0f0f0':'none'}}>
            <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:12}}>
              <Space>
                <Title level={4} style={{margin:0}}>{highlight(userName, search)}</Title>
                <Tag color={allHealthy?'green':'orange'}>{userSvcs.length} service{userSvcs.length>1?'s':''}</Tag>
              </Space>
              <Space>
                {isAdmin && <>
                  <Tooltip title="Clone all to another user">
                    <Button size="small" icon={<SwapOutlined/>} onClick={()=>{setCloneSource(userName);setCloneTarget('');setCloneOpen(true)}}>Clone All</Button>
                  </Tooltip>
                  <Button size="small" danger icon={<DeleteOutlined/>} disabled>Del</Button>
                </>}
              </Space>
            </div>

            <Collapse>
              {userSvcs.map((svc) => {
                const key = `${svc.user_name}-${svc.service_name}-${svc.label}`
                const containers = {...svc.healthy_containers, ...svc.unhealthy_containers, ...svc.missing_containers}
                const pwdVisible = visiblePwds[key] || false
                return (
                  <Collapse.Panel
                    key={key}
                    header={<Space>{getBadge(svc)}<Text strong>{highlight(svc.service_name, search)}</Text><Tag>{svc.label}</Tag>
                      {activeTasks[key] && <Button type="link" size="small" icon={<UnorderedListOutlined/>} onClick={(e)=>{e.stopPropagation();navigate('/tasks')}} style={{padding:0}}>Building...</Button>}
                    </Space>}
                    extra={
                      <Space onClick={e=>e.stopPropagation()}>
                        {isAdmin && <>
                          <Tooltip title="Start containers"><Button size="small" icon={<UpOutlined/>} onClick={()=>{
                            client.post(`/users/${svc.user_name}/${svc.service_name}/${svc.label}/up`).then(()=>{message.success('Starting...');loadServices()}).catch(e=>message.error(e.response?.data?.detail||'Failed'))
                          }}/></Tooltip>
                          <Tooltip title="Stop containers"><Button size="small" icon={<DownOutlined/>} onClick={()=>{
                            client.post(`/users/${svc.user_name}/${svc.service_name}/${svc.label}/down`).then(()=>{message.success('Stopping...');loadServices()}).catch(e=>message.error(e.response?.data?.detail||'Failed'))
                          }}/></Tooltip>
                          <Button size="small" onClick={async ()=>{
                            try {
                              const r = await client.post(`/users/${svc.user_name}/${svc.service_name}/${svc.label}/rebuild`,{})
                              const taskId = r.data?.task_id || r.data?.id
                              if (taskId) setActiveTasks(t=>({...t,[key]:taskId}))
                              message.success({content:<span>Rebuilding... {taskId && <Button type="link" size="small" icon={<UnorderedListOutlined/>} onClick={()=>navigate('/tasks')}>View Task</Button>}</span>,duration:5})
                              loadServices()
                            } catch(e:any) { message.error(e.response?.data?.detail||'Failed') }
                          }}>Rebuild</Button>
                          <Button size="small" icon={<RocketOutlined/>} onClick={async ()=>{
                            try {
                              const r = await client.post(`/users/${svc.user_name}/${svc.service_name}/${svc.label}/rebuild`,{no_cache:true})
                              const taskId = r.data?.task_id || r.data?.id
                              if (taskId) setActiveTasks(t=>({...t,[key]:taskId}))
                              message.success({content:<span>Redeploying... {taskId && <Button type="link" size="small" icon={<UnorderedListOutlined/>} onClick={()=>navigate('/tasks')}>View Task</Button>}</span>,duration:5})
                              loadServices()
                            } catch(e:any) { message.error(e.response?.data?.detail||'Failed') }
                          }}>Redeploy</Button>
                          <Tooltip title="Change password">
                            <Button size="small" icon={<KeyOutlined/>} onClick={()=>openPwdChange(svc.user_name,svc.service_name,svc.label)}/>
                          </Tooltip>
                          <Tooltip title="Duplicate for another user">
                            <Button size="small" icon={<CopyOutlined/>} onClick={()=>{
                              const t = prompt('Target user:')
                              if (t) client.post('/users/deploy',{user_name:t,service_name:svc.service_name,project_root:svc.service_name,compose_template_path:svc.compose_template_path,nginx_conf_template_path:svc.nginx_conf_template_path,label:svc.label,domain:'example.com',passwd:'default123',use_global_proxy:false}).then(()=>{message.success('Duplicated');loadServices()}).catch(e=>message.error(e.response?.data?.detail||'Failed'))
                            }}>Dup</Button>
                          </Tooltip>
                          <Popconfirm title="Delete?" onConfirm={()=>{
                            client.delete(`/users/${svc.user_name}/${svc.service_name}/${svc.label}`).then(()=>{message.success('Deleted');loadServices()}).catch(()=>message.error('Failed'))
                          }}>
                            <Button size="small" danger icon={<DeleteOutlined/>}/>
                          </Popconfirm>
                        </>}
                      </Space>
                    }
                  >
                    <Space direction="vertical" style={{width:'100%'}} size="small">
                      <div>
                        <Text strong>URL: </Text>
                        <a href={svc.url || `http://${svc.service_name}-${svc.user_name}-${svc.label}.example.com`} target="_blank" rel="noopener noreferrer">
                          <Text code style={{color:'#1677ff'}}>{svc.url || `http://${svc.service_name}-${svc.user_name}-${svc.label}.example.com`}</Text>
                        </a>
                        <Button size="small" type="link" icon={<LinkOutlined/>} onClick={()=>testCurl(svc)}>Test</Button>
                        {svc.has_auth && <Tag color="blue" style={{marginLeft:4}}>Auth</Tag>}
                      </div>
                      <div><Text strong>Containers: </Text>
                        <Space wrap>{Object.entries(containers).map(([n,s])=><Tag key={n} color={String(s).toLowerCase().includes('running')||String(s).toLowerCase().includes('up')?'green':'orange'}>{n}: {String(s)}</Tag>)}</Space>
                      </div>
                      <div><Text strong>Deployment Files: </Text></div>
                      <div style={{paddingLeft:16}}>
                        {svc.compose_template_path && <div style={{marginBottom:4}}>
                          <Tag color="blue">compose</Tag>
                          <Text code style={{cursor:'pointer',color:'#1677ff'}} onClick={()=>navigate(`/services/${svc.service_name}?file=${svc.compose_template_path?.split('/').pop()}`)}>
                            {svc.compose_template_path?.split('/').pop()}
                          </Text>
                          <Text type="secondary" style={{fontSize:11}}> (template)</Text>
                        </div>}
                        {svc.nginx_conf_template_path && <div style={{marginBottom:4}}>
                          <Tag color="purple">nginx</Tag>
                          <Text code style={{cursor:'pointer',color:'#1677ff'}} onClick={()=>navigate(`/services/${svc.service_name}?file=${svc.nginx_conf_template_path?.split('/').pop()}`)}>
                            {svc.nginx_conf_template_path?.split('/').pop()}
                          </Text>
                          <Text type="secondary" style={{fontSize:11}}> (template)</Text>
                        </div>}
                        <div style={{marginBottom:4}}>
                          <Tag color="green">env</Tag>
                          <Text code style={{cursor:'pointer',color:'#1677ff'}} onClick={()=>navigate(`/services/${svc.service_name}?file=.env`)}>
                            .env
                          </Text>
                          <Text type="secondary" style={{fontSize:11}}> (per-user)</Text>
                        </div>
                        <div style={{marginBottom:4}}>
                          <Tag color="orange">compose</Tag>
                          <Text code style={{cursor:'pointer',color:'#1677ff'}} onClick={()=>navigate(`/services/${svc.service_name}?file=docker-compose.yml`)}>
                            docker-compose.yml
                          </Text>
                          <Text type="secondary" style={{fontSize:11}}> (generated per-user)</Text>
                        </div>
                        <div style={{marginBottom:4}}>
                          <Tag color="purple">nginx</Tag>
                          <Text code style={{cursor:'pointer',color:'#1677ff'}} onClick={()=>navigate(`/services/${svc.service_name}?file=nginx.conf`)}>
                            nginx.conf
                          </Text>
                          <Text type="secondary" style={{fontSize:11}}> (generated per-user)</Text>
                        </div>
                        <div style={{marginBottom:4}}>
                          <Tag color="red">ssl</Tag>
                          <Text code style={{color:'#666'}}>
                            ssl/{svc.service_name}-{svc.user_name}-{svc.label}.crt
                          </Text>
                          <Text type="secondary" style={{fontSize:11}}> (managed by provision-nginx)</Text>
                        </div>
                      </div>
                      {(svc as any).volumes && Object.keys((svc as any).volumes).length>0 && <div><Text strong>Volumes: </Text><Space wrap>{Object.entries((svc as any).volumes).map(([k,v]:[string,any])=><Tag key={k}>{k}: {String(v)}</Tag>)}</Space></div>}
                    </Space>
                  </Collapse.Panel>
                )
              })}
            </Collapse>
          </div>
        )})
      )}

      <DeployForm open={deployOpen} onClose={()=>setDeployOpen(false)} onDeployed={()=>{setDeployOpen(false);loadServices()}}/>

      {/* Password change modal */}
      <Modal title="Change Service Password" open={pwdModalOpen} onCancel={()=>setPwdModalOpen(false)}
        onOk={handlePwdChange} confirmLoading={pwdLoading} okText="Change">
        <Space direction="vertical" style={{width:'100%'}} size="middle">
          <div><Text strong>Service: </Text>{pwdTarget?.service}/{pwdTarget?.user}/{pwdTarget?.label}</div>
          <Input.Password prefix={<KeyOutlined/>} placeholder="New password" value={newPassword} onChange={e=>setNewPassword(e.target.value)}/>
        </Space>
      </Modal>

      {/* Clone All modal */}
      <Modal title="Clone All Services" open={cloneOpen} onCancel={()=>setCloneOpen(false)}
        onOk={handleCloneAll} confirmLoading={cloneLoading} okText="Clone">
        <Space direction="vertical" style={{width:'100%'}} size="middle">
          <div><Text strong>Source: </Text><Tag>{cloneSource}</Tag></div>
          <div>
            <Text strong>Target User: </Text>
            <Input placeholder="e.g. bob" value={cloneTarget} onChange={e=>setCloneTarget(e.target.value)}/>
          </div>
          <Text type="secondary">All services from {cloneSource} will be cloned to the target user.</Text>
        </Space>
      </Modal>
    </div>
  )
}
