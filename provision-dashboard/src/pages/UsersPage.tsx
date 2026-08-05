import { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Typography, Card, Tag, Space, Button, Empty, Spin, message, Input, Collapse, Badge, Tooltip, Popconfirm, Modal, Drawer, Checkbox } from 'antd'
import { RocketOutlined, ReloadOutlined, EyeOutlined, EyeInvisibleOutlined, SearchOutlined, CaretRightOutlined, PauseOutlined, DeleteOutlined, CopyOutlined, LinkOutlined, KeyOutlined, SwapOutlined, UnorderedListOutlined, DashboardOutlined, GlobalOutlined } from '@ant-design/icons'
import Editor from '@monaco-editor/react'
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
  status?: string  // "building" | "running" | "unknown"
}

export default function UsersPage() {
  const { admin } = useAuth()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const isAdmin = admin?.role === 'admin'
  const [services, setServices] = useState<ServiceInstance[]>([])
  const [loading, setLoading] = useState(true)
  const deployParam = searchParams.get('deploy') || ''
  const [deployOpen, setDeployOpen] = useState(!!deployParam)
  const [preselectedDeploy, setPreselectedDeploy] = useState(deployParam || undefined)
  const [search, setSearch] = useState('')
  const [visiblePwds, setVisiblePwds] = useState<Record<string,boolean>>({})

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

  // Deployment file editor drawer
  const [editorOpen, setEditorOpen] = useState(false)
  const [editorFile, setEditorFile] = useState<{user:string;service:string;label:string;fileType:string;filename:string;path:string}|null>(null)
  const [editorContent, setEditorContent] = useState('')
  const [editorOriginal, setEditorOriginal] = useState('')
  const [editorLoading, setEditorLoading] = useState(false)
  const [editorSaving, setEditorSaving] = useState(false)

  // Registration times cache: key → unix timestamp
  const [regTimes, setRegTimes] = useState<Record<string, number|null>>({})
  // File modification times cache: key → unix timestamp
  const [fileModTimes, setFileModTimes] = useState<Record<string, number|null>>({})
  // Track which services need redeploy (files modified after registration)
  const [needsRedeploy, setNeedsRedeploy] = useState<Record<string, boolean>>({})

  // Volume usage cache: key → {volumes: {...}, user_data_dir: string}
  const [volumeUsage, setVolumeUsage] = useState<Record<string, any>>({})

  // Container resource stats cache: key → {cpu_percent, mem_usage_mb, mem_total_mb, disk_usage_mb}
  const [resourceStats, setResourceStats] = useState<Record<string, {cpu: string, mem: string, disk: string}>>({})

  // Batch selection state
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set())
  const [batchLoading, setBatchLoading] = useState(false)

  // Rebuild confirmation modal
  const [rebuildTarget, setRebuildTarget] = useState<{user:string;service:string;label:string;noCache:boolean}|null>(null)
  const [rebuildProxy, setRebuildProxy] = useState(false)
  const [rebuildLoading, setRebuildLoading] = useState(false)

  // Track which panels are currently expanded (for container-status refresh)
  const [expandedPanels, setExpandedPanels] = useState<string[]>([])
  // Timer ref for container-status refresh
  const refreshTimersRef = useRef<Record<string, ReturnType<typeof setInterval>>>({})
  const servicesRef = useRef(services)
  servicesRef.current = services

  // Nginx ports from system status (used for URL construction)
  const [nginxHttpPort, setNginxHttpPort] = useState(80)
  const [nginxHttpsPort, setNginxHttpsPort] = useState(443)

  // Fetch nginx ports once on mount
  useEffect(() => {
    client.get('/system/status').then(({data}) => {
      if (data.nginx_http_port) setNginxHttpPort(data.nginx_http_port)
      if (data.nginx_https_port) setNginxHttpsPort(data.nginx_https_port)
    }).catch(() => {})
  }, [])

  // Build service URL with correct port
  const buildServiceUrl = useCallback((svc: ServiceInstance): string => {
    if (svc.url) return svc.url
    const host = `${svc.service_name}-${svc.user_name}-${svc.label}.localhost`
    return `http://${host}${nginxHttpPort !== 80 ? ':' + nginxHttpPort : ''}`
  }, [nginxHttpPort])

  // Parse "user-service-label" key (service may contain hyphens like "example-mcp")
  const parseServiceKey = (key: string) => {
    const parts = key.split('-')
    return { user: parts[0], service: parts.slice(1, -1).join('-'), label: parts[parts.length - 1] }
  }

  // Container-status refresh: when a panel is expanded, re-fetch services every 5s
  // so container tags stay up-to-date without full page reload
  useEffect(() => {
    const timers = refreshTimersRef.current
    for (const k of Object.keys(timers)) {
      if (!expandedPanels.includes(k)) { clearInterval(timers[k]); delete timers[k] }
    }
    for (const key of expandedPanels) {
      if (timers[key]) continue
      timers[key] = setInterval(() => {
        refreshServices()
      }, 5000)
    }
    return () => { for (const t of Object.values(refreshTimersRef.current)) clearInterval(t); refreshTimersRef.current = {} }
  }, [expandedPanels])

  useEffect(() => { loadServices() }, [])

  useEffect(() => {
    if (services.length === 0) return
    // Check registration times for each service
    const checkTimes = async () => {
      for (const svc of services) {
        const key = `${svc.user_name}-${svc.service_name}-${svc.label}`
        if (regTimes[key] !== undefined) continue // Already fetched
        try {
          const { data } = await client.get(`/users/${svc.user_name}/${svc.service_name}/${svc.label}/registration-time`)
          const rt = data.registration_time || null
          setRegTimes(prev => ({...prev, [key]: rt}))
        } catch { /* ignore */ }
      }
    }
    checkTimes()
  }, [services])

  // Fetch volume disk usage for a service instance
  const fetchVolumeUsage = async (user: string, service: string, label: string) => {
    const key = `${user}-${service}-${label}`
    if (volumeUsage[key] !== undefined) return
    try {
      const { data } = await client.get(`/users/${user}/${service}/${label}/volume-usage`)
      setVolumeUsage(prev => ({...prev, [key]: data}))
      // Also populate disk info into resource stats
      if (data.volumes) {
        const volumes = data.volumes
        const totalBytes = Object.values(volumes).reduce((sum: number, v: any) => sum + (v.size_bytes || 0), 0)
        const diskStr = totalBytes > 1073741824 ? (totalBytes / 1073741824).toFixed(1) + 'GB' : totalBytes > 1048576 ? (totalBytes / 1048576).toFixed(0) + 'MB' : (totalBytes / 1024).toFixed(0) + 'KB'
        setResourceStats(prev => ({...prev, [key]: {...(prev[key] || {cpu:'',mem:''}), disk: totalBytes > 0 ? diskStr : ''}}))
      }
    } catch { /* ignore */ }
  }

  // Check file modification times for deployment files
  const checkFileModTimes = useCallback(async (user: string, service: string, label: string) => {
    const key = `${user}-${service}-${label}`
    try {
      const { data } = await client.get(`/users/${user}/${service}/${label}/deployment-files`)
      let latestMod: number | null = null
      for (const f of (data.files || [])) {
        if (f.exists && f.modified_at) {
          if (latestMod === null || f.modified_at > latestMod) {
            latestMod = f.modified_at
          }
        }
      }
      setFileModTimes(prev => ({...prev, [key]: latestMod}))
      // Check if files were modified after registration
      const regTime = regTimes[key]
      if (regTime && latestMod && latestMod > regTime) {
        setNeedsRedeploy(prev => ({...prev, [key]: true}))
      } else {
        setNeedsRedeploy(prev => ({...prev, [key]: false}))
      }
    } catch { /* ignore */ }
  }, [regTimes])

  // Fetch docker stats for container resource usage
  const fetchResourceStats = useCallback(async () => {
    if (services.length === 0) return
    try {
      const { data: statsData } = await client.get('/system/stats?detail=true')
      const containers = statsData.containers || []
      const newStats: Record<string, {cpu: string, mem: string, disk: string}> = {}

      for (const svc of services) {
        const key = `${svc.user_name}-${svc.service_name}-${svc.label}`
        const prefix = `${svc.service_name}-user_${svc.user_name}-${svc.label}-`
        const svcContainers = containers.filter((c: any) => c.name && c.name.startsWith(prefix))

        if (svcContainers.length > 0) {
          const totalCpu = svcContainers.reduce((sum: number, c: any) => {
            const cpuStr = c.cpu_percent || c.cpu || '0'
            return sum + parseFloat(String(cpuStr).replace('%', ''))
          }, 0)
          const totalMemMb = svcContainers.reduce((sum: number, c: any) => {
            const memStr = c.mem_usage_mb || (c.mem_usage || '0')
            return sum + parseFloat(String(memStr))
          }, 0)
          newStats[key] = {
            cpu: isNaN(totalCpu) ? '' : totalCpu.toFixed(1) + '%',
            mem: isNaN(totalMemMb) ? '' : (totalMemMb > 1024 ? (totalMemMb / 1024).toFixed(1) + 'GB' : totalMemMb.toFixed(0) + 'MB'),
            disk: '' // Will be filled from volume usage
          }
        }
      }
      setResourceStats(prev => ({...prev, ...newStats}))
    } catch { /* ignore */ }
  }, [services])

  // When services load, fetch resource stats and check file mod times
  useEffect(() => {
    for (const svc of services) {
      const key = `${svc.user_name}-${svc.service_name}-${svc.label}`
      if (fileModTimes[key] === undefined) {
        checkFileModTimes(svc.user_name, svc.service_name, svc.label)
      }
      if (isAdmin && volumeUsage[key] === undefined) {
        fetchVolumeUsage(svc.user_name, svc.service_name, svc.label)
      }
    }
    if (isAdmin && services.length > 0) {
      fetchResourceStats()
      // Poll resource stats every 15s for live CPU/RAM updates
      const interval = setInterval(fetchResourceStats, 15000)
      return () => clearInterval(interval)
    }
  }, [services, checkFileModTimes])

  // Open deployment file editor
  const openFileEditor = async (user: string, service: string, label: string, fileType: string, filename: string) => {
    setEditorLoading(true)
    setEditorFile({ user, service, label, fileType, filename, path: '' })
    setEditorOpen(true)
    try {
      const { data } = await client.get(`/users/${user}/${service}/${label}/deployment-files/${fileType}`)
      setEditorContent(data.content || '')
      setEditorOriginal(data.content || '')
      setEditorFile(prev => prev ? {...prev, path: data.path || ''} : null)
      if (data.source_fallback) {
        message.info('Loaded from source template. Save to create the per-user deployment file.')
      } else if (!data.exists) {
        message.info('File does not exist yet. Create it by saving.')
      }
    } catch (err: any) {
      if (err.response?.status === 404) {
        setEditorContent('')
        setEditorOriginal('')
        message.info('File does not exist yet. Create it by saving.')
      } else {
        message.error('Failed to load file')
        setEditorOpen(false)
      }
    } finally {
      setEditorLoading(false)
    }
  }

  // Save deployment file
  const saveFile = async () => {
    if (!editorFile) return
    setEditorSaving(true)
    try {
      await client.put(`/users/${editorFile.user}/${editorFile.service}/${editorFile.label}/deployment-files/${editorFile.fileType}`, { content: editorContent })
      message.success('File saved')
      setEditorOriginal(editorContent)
      // Re-check modification times
      checkFileModTimes(editorFile.user, editorFile.service, editorFile.label)
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to save')
    } finally {
      setEditorSaving(false)
    }
  }

  // Get language for Monaco based on file type
  const getEditorLanguage = (fileType: string) => {
    switch (fileType) {
      case 'compose': return 'yaml'
      case 'nginx': return 'nginx'
      case 'env': return 'shell'
      default: return 'plaintext'
    }
  }

  const isEndUser = (admin as any)?.user_type === 'end_user'
  const endUserViewer = isEndUser && admin?.role !== 'admin'

  // Toggle a single service selection
  const toggleSelect = (key: string) => {
    setSelectedKeys(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key); else next.add(key)
      return next
    })
  }

  // Toggle select all for a user group
  const toggleSelectAll = (userName: string) => {
    const userKeys = services.filter(s => s.user_name === userName).map(s => `${s.user_name}-${s.service_name}-${s.label}`)
    const allSelected = userKeys.every(k => selectedKeys.has(k))
    setSelectedKeys(prev => {
      const next = new Set(prev)
      if (allSelected) { userKeys.forEach(k => next.delete(k)) }
      else { userKeys.forEach(k => next.add(k)) }
      return next
    })
  }

  // Batch action: start/stop/rebuild/remove selected services
  const batchAction = async (action: string) => {
    setBatchLoading(true)
    const keys = Array.from(selectedKeys)
    try {
      for (const key of keys) {
        const {user, service, label} = parseServiceKey(key)
        switch (action) {
          case 'stop': await client.post(`/users/${user}/${service}/${label}/down`); break
          case 'start': await client.post(`/users/${user}/${service}/${label}/up`); break
          case 'rebuild': await client.post(`/users/${user}/${service}/${label}/rebuild`, {no_cache: true}); break
          case 'remove': await client.delete(`/users/${user}/${service}/${label}`); break
        }
      }
      message.success(`Batch ${action}: ${keys.length} service(s) processed`)
      setSelectedKeys(new Set())
      refreshServices()
    } catch (err: any) {
      message.error(err.response?.data?.detail || `Batch ${action} failed`)
    } finally {
      setBatchLoading(false)
    }
  }

  const _fetchServices = async (): Promise<ServiceInstance[]> => {
    const { data } = await client.get('/users')
    const users = data.users || data.user_status || []
    const all: ServiceInstance[] = []
    for (const u of users) {
      if (endUserViewer && admin?.email && u.user_name !== admin.email) continue
      for (const s of (u.healthy_services||[])) all.push({...s, user_name: u.user_name})
      for (const s of (u.unhealthy_services||[])) all.push({...s, user_name: u.user_name})
      for (const s of (u.missing_services||[])) all.push({...s, user_name: u.user_name})
    }
    return all
  }

  const loadServices = async () => {
    setLoading(true)
    try {
      setServices(await _fetchServices())
    } catch (err: any) { message.error('Failed to load services') }
    finally { setLoading(false) }
  }

  // Silent refresh — no loading spinner, for auto-polling
  const refreshServices = useCallback(async () => {
    try {
      setServices(await _fetchServices())
    } catch { /* silent */ }
  }, [])

  // Auto-poll every 10s — silent refresh, no loading spinner
  useEffect(() => {
    const interval = setInterval(refreshServices, 10000)
    return () => clearInterval(interval)
  }, [refreshServices])

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
    // Building: containers don't exist yet, don't show misleading "down" count
    if (s.status === 'building') return <Badge status="processing" text="Building..."/>
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
      refreshServices()
    } catch (err: any) { message.error(err.response?.data?.detail || 'Failed') }
    finally { setCloneLoading(false) }
  }

  const handleRebuildConfirm = async () => {
    if (!rebuildTarget) return
    const { user, service, label, noCache } = rebuildTarget
    const key = `${user}-${service}-${label}`
    setRebuildLoading(true)
    // Optimistic: mark as building immediately
    setServices(prev => prev.map(s => s.user_name === user && s.service_name === service && s.label === label ? {...s, healthy_containers:{}, unhealthy_containers:{}, missing_containers:{}, status:'building'} : s))
    try {
      const payload: any = {}
      if (noCache) payload.no_cache = true
      if (rebuildProxy) payload.use_global_proxy = true
      const r = await client.post(`/users/${user}/${service}/${label}/rebuild`, payload)
      const taskId = r.data?.task_id || r.data?.id
      message.success({content:<span>{noCache ? 'Redeploying' : 'Rebuilding'}... {taskId && <Button type="link" size="small" icon={<UnorderedListOutlined/>} onClick={()=>navigate('/tasks')}>View Task</Button>}</span>,duration:5})
      if (noCache) setNeedsRedeploy(prev => ({...prev, [key]: false}))
    } catch(e:any) { message.error(e.response?.data?.detail||'Failed') }
    setRebuildTarget(null)
    setRebuildProxy(false)
    setRebuildLoading(false)
    setTimeout(refreshServices, 2000)
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

      {selectedKeys.size > 0 && isAdmin && (
        <Card size="small" style={{marginBottom:12,background:'#fffbe6',borderColor:'#faad14'}}>
          <Space>
            <Text strong>{selectedKeys.size} selected</Text>
            <Button size="small" loading={batchLoading} onClick={()=>batchAction('stop')}>Stop</Button>
            <Button size="small" loading={batchLoading} onClick={()=>batchAction('start')}>Start</Button>
            <Button size="small" loading={batchLoading} icon={<RocketOutlined/>} onClick={()=>batchAction('rebuild')}>Rebuild</Button>
            <Popconfirm title={`Remove ${selectedKeys.size} service(s)?`} onConfirm={()=>batchAction('remove')}>
              <Button size="small" danger loading={batchLoading}>Remove</Button>
            </Popconfirm>
            <Button size="small" onClick={()=>setSelectedKeys(new Set())}>Clear</Button>
          </Space>
        </Card>
      )}

      {loading ? <Spin/> : Object.keys(grouped).length===0 ? (
        <Card><Empty description={search?"No matches":"No services deployed"}/></Card>
      ) : (
        Object.entries(grouped).map(([userName, userSvcs], idx) => {
          const allHealthy = userSvcs.every(s => s.status === 'building' || (Object.keys(s.unhealthy_containers||{}).length===0 && Object.keys(s.missing_containers||{}).length===0))
          const healthyCount = userSvcs.filter(s => Object.keys(s.healthy_containers||{}).length > 0 && Object.keys(s.unhealthy_containers||{}).length===0 && Object.keys(s.missing_containers||{}).length===0).length
          const buildingCount = userSvcs.filter(s => s.status === 'building').length
          const unhealthyCount = userSvcs.length - healthyCount - buildingCount
          return (
          <div key={userName} style={{marginBottom: idx<Object.keys(grouped).length-1?32:0, paddingBottom: idx<Object.keys(grouped).length-1?16:0, borderBottom: idx<Object.keys(grouped).length-1?'1px solid #f0f0f0':'none'}}>
            <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:12}}>
              <Space>
                {isAdmin && <Checkbox
                  checked={userSvcs.every(s => selectedKeys.has(`${s.user_name}-${s.service_name}-${s.label}`))}
                  indeterminate={userSvcs.some(s => selectedKeys.has(`${s.user_name}-${s.service_name}-${s.label}`)) && !userSvcs.every(s => selectedKeys.has(`${s.user_name}-${s.service_name}-${s.label}`))}
                  onChange={() => toggleSelectAll(userName)}
                />}
                <Title level={4} style={{margin:0}}>{highlight(userName, search)}</Title>
                <Tag color={allHealthy?'green':'orange'}>{userSvcs.length} service{userSvcs.length>1?'s':''}{unhealthyCount > 0 ? ` (${healthyCount} healthy, ${unhealthyCount} unhealthy)` : ` (all healthy)`}</Tag>
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

            <Collapse activeKey={expandedPanels} onChange={(keys) => setExpandedPanels(typeof keys === 'string' ? [keys] : keys)}>
              {userSvcs.map((svc) => {
                const key = `${svc.user_name}-${svc.service_name}-${svc.label}`
                const containers = {...svc.healthy_containers, ...svc.unhealthy_containers, ...svc.missing_containers}
                const pwdVisible = visiblePwds[key] || false
                return (
                  <Collapse.Panel
                    key={key}
                    header={<Space>
                      {isAdmin && <Checkbox checked={selectedKeys.has(key)} onChange={()=>toggleSelect(key)} onClick={(e:any)=>e.stopPropagation()}/>}
                      {getBadge(svc)}
                      <Text strong>{highlight(svc.service_name, search)}</Text>
                      <Tag>{svc.label}</Tag>
                      {resourceStats[key] && Object.keys(svc.healthy_containers||{}).length > 0 && (
                        <span style={{marginLeft:8,display:'inline-flex',gap:6,alignItems:'center'}}>
                          {resourceStats[key].cpu && <Tag color="blue" style={{fontSize:11,margin:0}}><DashboardOutlined/> CPU {resourceStats[key].cpu}</Tag>}
                          {resourceStats[key].mem && <Tag color="purple" style={{fontSize:11,margin:0}}>RAM {resourceStats[key].mem}</Tag>}
                        </span>
                      )}
                    </Space>}
                    extra={
                      <Space onClick={e=>e.stopPropagation()}>
                        {isAdmin && <>
                          {svc.status !== 'building' && <Tooltip title={Object.keys(svc.healthy_containers||{}).length > 0 ? 'Stop containers' : 'Start containers'}>
                            <Button size="small" 
                              icon={Object.keys(svc.healthy_containers||{}).length > 0 ? <PauseOutlined/> : <CaretRightOutlined/>}
                              type={Object.keys(svc.healthy_containers||{}).length > 0 ? 'default' : 'primary'}
                              onClick={()=>{
                                const isRunning = Object.keys(svc.healthy_containers||{}).length > 0
                                const action = isRunning ? 'down' : 'up'
                                const label = isRunning ? 'Stopping...' : 'Starting...'
                                client.post(`/users/${svc.user_name}/${svc.service_name}/${svc.label}/${action}`)
                                  .then(()=>{message.success(label); refreshServices()})
                                  .catch(e=>message.error(e.response?.data?.detail||'Failed'))
                              }}
                            />
                          </Tooltip>}
                          <Button size="small" onClick={()=>{
                            setRebuildTarget({user:svc.user_name,service:svc.service_name,label:svc.label,noCache:false})
                            setRebuildProxy(false)
                          }}>Rebuild</Button>
                          <Tooltip title="Redeploy service with no-cache rebuild">
                            <Button size="small" icon={<RocketOutlined/>} 
                              className={needsRedeploy[key] ? 'redeploy-blink' : ''}
                              style={needsRedeploy[key] ? {borderColor:'#faad14',color:'#faad14'} : {}}
                              onClick={()=>{
                            setRebuildTarget({user:svc.user_name,service:svc.service_name,label:svc.label,noCache:true})
                            setRebuildProxy(false)
                          }}>Redeploy</Button>
                          </Tooltip>
                          <Tooltip title="Change password">
                            <Button size="small" icon={<KeyOutlined/>} onClick={()=>openPwdChange(svc.user_name,svc.service_name,svc.label)}/>
                          </Tooltip>
                          <Tooltip title="Duplicate for another user">
                            <Button size="small" icon={<CopyOutlined/>} onClick={()=>{
                              setPreselectedDeploy(svc.service_name)
                              setDeployOpen(true)
                            }}>Dup</Button>
                          </Tooltip>
                          <Popconfirm title="Delete?" onConfirm={()=>{
                            client.delete(`/users/${svc.user_name}/${svc.service_name}/${svc.label}`).then(()=>{message.success('Deleted');refreshServices()}).catch(()=>message.error('Failed'))
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
                        <a href={buildServiceUrl(svc)} target="_blank" rel="noopener noreferrer">
                          <Text code style={{color:'#1677ff'}}>{buildServiceUrl(svc)}</Text>
                        </a>
                        {svc.has_auth && <Tag color="blue" style={{marginLeft:4}}>Auth</Tag>}
                      </div>
                      <div><Text strong>Containers: </Text>
                        <Space wrap>{Object.entries(containers).map(([n,s])=>{
                          const status = String(s).toLowerCase();
                          const color = status.includes('up') || status.includes('healthy') ? 'green'
                            : status.includes('unhealthy') ? 'orange'
                            : 'red';
                          return <Tag key={n} color={color}>{n}: {String(s)}</Tag>
                        })}</Space>
                      </div>
                      <div><Text strong>Deployment Files: </Text></div>
                      <div style={{paddingLeft:16}}>
                        {/* .env file */}
                        {(() => {
                          const envFile = `.env.${svc.user_name}.${svc.label}`
                          return <div style={{marginBottom:4}}>
                            <Text>.env: </Text>
                            <Text code style={{cursor:'pointer',color:'#1677ff',textDecoration:'underline'}}
                              onClick={() => openFileEditor(svc.user_name, svc.service_name, svc.label, 'env', envFile)}>{envFile}</Text>
                            <Text type="secondary" style={{fontSize:11}}> (in PROVISION/source_projects/{svc.service_name} dir)</Text>
                          </div>
                        })()}
                        {/* generated compose file */}
                        {(() => {
                          const composeFile = `docker-compose.user-${svc.user_name}.${svc.label}.yml`
                          return <div style={{marginBottom:4}}>
                            <Text>compose: </Text>
                            <Text code style={{cursor:'pointer',color:'#1677ff',textDecoration:'underline'}}
                              onClick={() => openFileEditor(svc.user_name, svc.service_name, svc.label, 'compose', composeFile)}>{composeFile}</Text>
                            <Text type="secondary" style={{fontSize:11}}> (in PROVISION/source_projects/{svc.service_name} dir)</Text>
                          </div>
                        })()}
                        {/* nginx conf file */}
                        {(() => {
                          const nginxConfPath = svc.nginx_conf_template_path || ''
                          const nginxBase = nginxConfPath.split('/').pop()?.replace('.j2', '') || svc.service_name
                          const nginxConfFile = `${nginxBase}.user-${svc.user_name}.${svc.label}.nginx.conf`
                          return <div style={{marginBottom:4}}>
                            <Text>nginx conf: </Text>
                            <Text code style={{cursor:'pointer',color:'#1677ff',textDecoration:'underline'}}
                              onClick={() => openFileEditor(svc.user_name, svc.service_name, svc.label, 'nginx', nginxConfFile)}>{nginxConfFile}</Text>
                            <Text type="secondary" style={{fontSize:11}}> (in PROVISION/generated dir)</Text>
                          </div>
                        })()}
                        {/* ssl */}
                        {svc.url && svc.url.startsWith('https') && (() => {
                          const domain = new URL(svc.url).hostname.split('.').slice(-2).join('.')
                          return <div style={{marginBottom:4}}>
                            <Text>ssl: </Text>
                            <Text code>fullchain.pem, privkey.pem</Text>
                            <Text type="secondary" style={{fontSize:11}}> (in PROVISION/ssl/{domain} dir)</Text>
                          </div>
                        })()}
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

      <DeployForm open={deployOpen} preselectedService={preselectedDeploy} onClose={()=>{setDeployOpen(false); setPreselectedDeploy(undefined); if(deployParam) setSearchParams({})}} onDeployed={(taskId: string, user: string, service: string, label: string)=>{
        setDeployOpen(false); setPreselectedDeploy(undefined); if(deployParam) setSearchParams({});
        // Optimistic update: immediately show the new service as "building"
        const optimisticEntry: ServiceInstance = {
          user_name: user, service_name: service, label: label,
          healthy_containers: {}, unhealthy_containers: {}, missing_containers: {},
          status: 'building',
        }
        setServices(prev => {
          // Remove any existing entry for this service (in case of redeploy)
          const filtered = prev.filter(s => !(s.user_name === user && s.service_name === service && s.label === label))
          return [...filtered, optimisticEntry]
        })
        // Then also do a real reload (will update once registry is written)
        setTimeout(refreshServices, 2000)
      }}/>

      {/* Password change modal */}
      <Modal title="Change Service Password" open={pwdModalOpen} onCancel={()=>setPwdModalOpen(false)}
        onOk={handlePwdChange} confirmLoading={pwdLoading} okText="Change">
        <Space direction="vertical" style={{width:'100%'}} size="middle">
          <div><Text strong>Service: </Text>{pwdTarget?.service}/{pwdTarget?.user}/{pwdTarget?.label}</div>
          <Input.Password prefix={<KeyOutlined/>} placeholder="New password" value={newPassword} onChange={e=>setNewPassword(e.target.value)}/>
        </Space>
      </Modal>

      {/* Rebuild / Redeploy confirmation modal */}
      <Modal
        title={rebuildTarget?.noCache ? 'Redeploy Service' : 'Rebuild Service'}
        open={!!rebuildTarget}
        onCancel={() => { setRebuildTarget(null); setRebuildProxy(false) }}
        onOk={handleRebuildConfirm}
        confirmLoading={rebuildLoading}
        okText={rebuildTarget?.noCache ? 'Redeploy' : 'Rebuild'}
      >
        <Space direction="vertical" style={{width:'100%'}} size="middle">
          <div>
            <Text strong>Service: </Text>
            {rebuildTarget?.service}/{rebuildTarget?.user}/{rebuildTarget?.label}
          </div>
          {rebuildTarget?.noCache && (
            <div>
              <Text type="warning">No-cache rebuild — will rebuild Docker image from scratch.</Text>
            </div>
          )}
          <Checkbox checked={rebuildProxy} onChange={e => setRebuildProxy(e.target.checked)}>
            <Space>
              <GlobalOutlined />
              Use global proxy for this {rebuildTarget?.noCache ? 'redeploy' : 'rebuild'}
            </Space>
          </Checkbox>
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

      {/* Deployment File Editor Drawer */}
      <Drawer
        title={editorFile ? <Space><Text strong>{editorFile.filename}</Text><Tag>{editorFile.fileType}</Tag><Text type="secondary">for {editorFile.user}/{editorFile.service}/{editorFile.label}</Text></Space> : 'File Editor'}
        open={editorOpen}
        onClose={() => {
          if (editorContent !== editorOriginal) {
            Modal.confirm({
              title: 'Unsaved changes',
              content: 'You have unsaved changes. Discard them?',
              onOk: () => { setEditorOpen(false); setEditorFile(null); }
            })
          } else {
            setEditorOpen(false)
            setEditorFile(null)
          }
        }}
        width="80%"
        extra={
          <Space>
            {editorContent !== editorOriginal && <Tag color="orange">Modified</Tag>}
            <Button onClick={() => setEditorContent(editorOriginal)} disabled={editorContent === editorOriginal}>Reset</Button>
            <Button type="primary" onClick={saveFile} loading={editorSaving} disabled={editorContent === editorOriginal}>
              Save & Close
            </Button>
          </Space>
        }
      >
        {editorLoading ? <Spin /> : (
          <div style={{height:'calc(100vh - 180px)'}}>
            <Editor
              height="100%"
              language={getEditorLanguage(editorFile?.fileType || 'plaintext')}
              theme="vs-dark"
              value={editorContent}
              onChange={(val) => setEditorContent(val || '')}
              options={{
                minimap: { enabled: false },
                fontSize: 13,
                wordWrap: 'on',
                scrollBeyondLastLine: false,
                automaticLayout: true,
              }}
            />
          </div>
        )}
      </Drawer>
    </div>
  )
}
