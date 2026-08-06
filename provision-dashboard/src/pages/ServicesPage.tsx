import { useState, useEffect, useMemo } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import {
  Typography, Card, Table, Button, Modal, Form, Input,
  Space, message, Tag, Empty, Tabs, Spin, Checkbox, Upload,
  Tooltip, Select
} from 'antd'
import type { UploadProps } from 'antd'
import {
  PlusOutlined, DeleteOutlined, FolderOpenOutlined,
  GithubOutlined, UploadOutlined, InboxOutlined, FileAddOutlined,
  RobotOutlined
} from '@ant-design/icons'
import Editor, { DiffEditor } from '@monaco-editor/react'
import { useAuth } from '../hooks/useAuth'
import client from '../api/client'

const { Title, Text } = Typography

interface ServiceInfo {
  name: string; path: string; files: string[]
  has_compose_template: boolean; has_nginx_template: boolean
  has_dockerfile: boolean; active_users: number
  active_instances: string[]; created_at: string
  generated_files?: string[]
  template_files?: string[]
  recipes?: { path: string; label: string; is_root: boolean; template_files: string[] }[]
}

interface TableRow extends ServiceInfo {
  nameRowSpan: number
  recipePath: string
  recipeLabel: string
  recipeTemplates: string[]
  recipeGeneratedFiles: string[]
}

export default function ServicesPage() {
  const { name } = useParams<{ name?: string }>()
  const navigate = useNavigate()
  const { admin } = useAuth()
  const isAdmin = admin?.role === 'admin'
  const [services, setServices] = useState<ServiceInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [addModalOpen, setAddModalOpen] = useState(false)
  const [addLoading, setAddLoading] = useState(false)
  const [addMode, setAddMode] = useState<'git' | 'upload'>('git')
  const [form] = Form.useForm()
  const [proxyEnabled, setProxyEnabled] = useState(false)
  const [genLoading, setGenLoading] = useState<string | null>(null)
  const [missingFilesMap, setMissingFilesMap] = useState<Record<string, string[]>>({})  // service name being generated

  useEffect(() => { loadServices(); loadProxyStatus() }, [])

  const loadProxyStatus = async () => {
    try {
      const { data } = await client.get('/system/proxy')
      setProxyEnabled(data.has_active === true)
    } catch { /* proxy not configured */ }
  }

  const loadServices = async () => {
    setLoading(true)
    try {
      const { data } = await client.get('/services')
      const svcs = data.services || []
      setServices(svcs)
      // Check missing files for all services in parallel
      checkAllMissing(svcs)
    } catch (err: any) {
      message.error('Failed to load services')
    } finally { setLoading(false) }
  }

  const refreshServices = async () => {
    try {
      const { data } = await client.get('/services')
      const svcs = data.services || []
      setServices(svcs)
      checkAllMissing(svcs)
    } catch {}
  }

  const checkAllMissing = async (svcs: ServiceInfo[]) => {
    const map: Record<string, string[]> = {}
    const checks = svcs.flatMap(async (s) => {
      const recipes = s.recipes || []
      if (recipes.length > 1) {
        return recipes.map(async (r) => {
          const key = r.path ? `${s.name}@@${r.path}` : s.name
          try {
            const params: any = {}
            if (r.path) params.recipe_path = r.path
            const { data } = await client.get(`/services/${s.name}/check-missing-files`, { params })
            map[key] = data.missing || []
          } catch { map[key] = [] }
        })
      }
      try {
        const { data } = await client.get(`/services/${s.name}/check-missing-files`)
        map[s.name] = data.missing || []
      } catch { map[s.name] = [] }
    })
    await Promise.all(checks)
    setMissingFilesMap(map)
  }

  // Silent reload when coming back from project detail view
  useEffect(() => { if (!name) { refreshServices() } }, [name])

  const handleAdd = async (values: any) => {
    setAddLoading(true)
    try {
      await client.post('/services', { ...values, mode: addMode })
      message.success('Service created!')
      setAddModalOpen(false); form.resetFields(); loadServices()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to create service')
    } finally { setAddLoading(false) }
  }

  const handleDelete = async (serviceName: string) => {
    Modal.confirm({
      title: 'Delete Service',
      content: `Delete "${serviceName}"?`,
      okText: 'Delete', okType: 'danger',
      onOk: async () => {
        try {
          await client.delete(`/services/${serviceName}`)
          message.success('Deleted'); loadServices()
        } catch (err: any) {
          message.error(err.response?.data?.detail || 'Failed to delete')
        }
      },
    })
  }

  // Generate missing files via LLM for a service project
  const generateMissingFiles = async (serviceName: string, recipePath: string = '') => {
    const genKey = recipePath ? `${serviceName}@@${recipePath}` : serviceName
    setGenLoading(genKey)
    try {
      const params: any = {}
      if (recipePath) params.recipe_path = recipePath
      const { data: check } = await client.get(`/services/${serviceName}/check-missing-files`, { params })
      const missing: string[] = check.missing || []
      if (missing.length === 0) { message.info('All essential files are present'); return }
      
      const ctx = check.scan_context || { repo_description: `Service: ${serviceName}${recipePath ? ` (${recipePath})` : ''}` }
      const typeMap: Record<string, string> = {
        'docker-compose': 'docker_compose', 'nginx.conf': 'nginx_conf',
        '.env': 'env_file', 'Dockerfile': 'dockerfile',
      }
      const results: Record<string, string> = {}
      for (const ft of missing) {
        try {
          const { data: gen } = await client.post('/llm/generate', { type: typeMap[ft] || 'docker_compose', context: ctx })
          if (gen.generated_content) {
            const fn = ft === 'docker-compose' ? 'docker-compose.yml' : ft === 'nginx.conf' ? 'nginx.conf' : ft
            results[fn] = gen.generated_content
          }
        } catch { /* skip individual failures */ }
      }
      
      if (Object.keys(results).length > 0) {
        await client.post('/services/save-generated', { service_name: serviceName, files: results, recipe_path: recipePath })
        message.success(`LLM generated ${Object.keys(results).length} file(s) for ${serviceName}${recipePath ? ` @ ${recipePath}` : ''}`)
        refreshServices()
      } else {
        message.warning('LLM could not generate any files. Configure BYOK LLM in Settings.')
      }
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Generation failed')
    } finally { setGenLoading(null) }
  }

  // Flatten multi-recipe projects into table rows with rowSpan on Name
  // MUST be before the early return (React hooks rule)
  const tableData: TableRow[] = useMemo(() => {
    const rows: TableRow[] = []
    for (const s of services) {
      const recipes = s.recipes || []
      const allGenerated = s.generated_files || []
      if (recipes.length > 1) {
        // Collect all recipe subdirectory prefixes
        const recipePrefixes = recipes.map(r => r.path ? r.path + '/' : '')
        recipes.forEach((r, i) => {
          const prefix = r.path ? r.path + '/' : ''
          // Generated files belong to this recipe if they start with its prefix
          // Root recipe: files that don't start with any other recipe prefix
          const genFiles = r.path
            ? allGenerated.filter(f => f.startsWith(prefix))
            : allGenerated.filter(f => !recipePrefixes.some(p => p && f.startsWith(p)))
          rows.push({
            ...s,
            nameRowSpan: i === 0 ? recipes.length : 0,
            recipePath: r.path,
            recipeLabel: r.label,
            recipeTemplates: r.template_files,
            recipeGeneratedFiles: genFiles,
          })
        })
      } else {
        rows.push({
          ...s,
          nameRowSpan: 1,
          recipePath: '',
          recipeLabel: '',
          recipeTemplates: s.template_files || [],
          recipeGeneratedFiles: allGenerated,
        })
      }
    }
    return rows
  }, [services])

  if (name) return <ServiceDetailPage name={name} onBack={() => navigate('/services')} />

  const columns = [
    { title: 'Name', dataIndex: 'name', key: 'name',
      render: (t: string, r: TableRow) => ({
        children: <Space size={4}>
          <Button type="link" onClick={() => navigate(`/services/${t}`)}><FolderOpenOutlined /> {t}</Button>
          {isAdmin && r.nameRowSpan > 0 && <Button size="small" danger icon={<DeleteOutlined/>} onClick={()=>handleDelete(r.name)} style={{marginLeft:4}}/>}
        </Space>,
        props: { rowSpan: r.nameRowSpan },
      }) },
    { title: 'Templates', key: 'templates',
      render: (_:any, r: TableRow) => {
        const recipes = (r as any).recipes || []
        if (recipes.length > 1 && r.recipeLabel) {
          return <Space size={4} wrap>
            <Tag color="blue" style={{fontWeight:600}}>{r.recipeLabel}</Tag>
            {r.recipeTemplates.map(f => <Tag key={f} color="green" style={{cursor:'pointer'}} onClick={()=>navigate(`/services/${r.name}?file=${f}`)}>{f}</Tag>)}
          </Space>
        }
        const temps = r.template_files || []
        return <Space size={4} wrap>{temps.length>0 ? temps.map(f=><Tag key={f} color="green" style={{cursor:'pointer'}} onClick={()=>navigate(`/services/${r.name}?file=${f}`)}>{f}</Tag>) : <Tag>none</Tag>}</Space>
      }
    },
    { title: 'Generated Files', key: 'generated',
      render: (_:any, r: TableRow) => {
        const gens = r.recipeGeneratedFiles || r.generated_files || []
        return <Space size={4} wrap>{gens.length>0 ? gens.map(f=><Tag key={f} color="gold" style={{cursor:'pointer'}} onClick={()=>navigate(`/services/${r.name}?file=${f}`)}>{f}</Tag>) : <Tag>none</Tag>}</Space>
      }
    },
    { title: 'Actions', key: 'actions',
      render: (_:any, r: TableRow) => {
        const genKey = r.recipePath ? `${r.name}@@${r.recipePath}` : r.name
        const missing = missingFilesMap[genKey] ?? null
        const hasAll = missing !== null && missing.length === 0
        return <Tooltip title={missing === null ? 'Checking...' : hasAll ? 'No missing basic files, ready for deployment' : `Missing: ${missing.join(', ')}`}>
          <Button size="small" type="primary" icon={<RobotOutlined/>} disabled={hasAll} loading={genLoading === genKey} onClick={()=>generateMissingFiles(r.name, r.recipePath)}/>
        </Tooltip>
      } },
  ]

  return (
    <div>
      <div style={{display:'flex',justifyContent:'space-between',marginBottom:16}}>
        <Title level={3} style={{margin:0}}>Source Projects</Title>
        {isAdmin && <Button type="primary" icon={<PlusOutlined/>} onClick={()=>setAddModalOpen(true)}>Add Project</Button>}
      </div>
      <Card>
        {loading ? <Spin/> : services.length===0 ? <Empty description="No source projects yet"><Button type="primary" icon={<PlusOutlined/>} onClick={()=>setAddModalOpen(true)}>Add Project</Button></Empty> :
        <Table dataSource={tableData} columns={columns} rowKey={(r: TableRow) => r.recipePath ? `${r.name}@@${r.recipePath}` : r.name} pagination={false}/>}
      </Card>
      <Modal title="Add Source Project" open={addModalOpen} onCancel={()=>{setAddModalOpen(false);form.resetFields()}} footer={null} width={560}>
        <Tabs activeKey={addMode} onChange={(k)=>{setAddMode(k as any);form.resetFields()}} items={[
          { key:'git', label:<span><GithubOutlined/> From Git</span>, children:
            <Form form={form} layout="vertical" onFinish={handleAdd}>
              <Form.Item name="repo_url" label="Repository URL" rules={[{required:true}]}><Input placeholder="https://github.com/user/repo.git"/></Form.Item>
              <Form.Item name="branch" label="Branch" initialValue="main"><Input placeholder="main"/></Form.Item>
              <Form.Item name="name" label="Service Name" rules={[{required:true}]}><Input placeholder="myapp"/></Form.Item>
              <Form.Item name="use_proxy" valuePropName="checked">
                <Checkbox disabled={!proxyEnabled}>
                  Use global proxy for clone
                  {!proxyEnabled && <span style={{color:'#999',fontSize:12}}> (enable in Settings)</span>}
                </Checkbox>
              </Form.Item>
              <Button type="primary" htmlType="submit" loading={addLoading} block>Clone & Create</Button>
            </Form> },
          { key:'upload', label:<span><UploadOutlined/> Upload Zip</span>, children:
            <UploadZipForm form={form} addLoading={addLoading} setAddLoading={setAddLoading}
              onSuccess={()=>{setAddModalOpen(false);form.resetFields();loadServices()}} /> },
        ]}/>
      </Modal>
    </div>
  )
}

// ---- Upload Zip Form (file selector instead of base64 paste) ----
function UploadZipForm({ form, addLoading, setAddLoading, onSuccess }: {
  form: any; addLoading: boolean; setAddLoading: (v: boolean) => void; onSuccess: () => void
}) {
  const [zipBase64, setZipBase64] = useState<string>('')

  const uploadProps: UploadProps = {
    accept: '.zip',
    maxCount: 1,
    beforeUpload: (file) => {
      const reader = new FileReader()
      reader.onload = () => {
        const result = reader.result as string
        // Strip data:application/zip;base64, prefix if present
        const b64 = result.includes('base64,') ? result.split('base64,')[1] : result
        setZipBase64(b64)
        form.setFieldsValue({ zip_content: b64 })
        message.success(`Selected: ${file.name}`)
      }
      reader.readAsDataURL(file)
      return false // Prevent auto-upload
    },
    onRemove: () => {
      setZipBase64('')
      form.setFieldsValue({ zip_content: '' })
    },
  }

  const handleUpload = async (values: any) => {
    if (!zipBase64 && !values.files) {
      message.warning('Please select a zip file or provide file contents')
      return
    }
    setAddLoading(true)
    try {
      const payload: any = { name: values.name, mode: 'upload' }
      if (zipBase64) {
        payload.zip_content = zipBase64
      }
      if (values.files) {
        try { payload.files = JSON.parse(values.files) } catch { /* not JSON */ }
      }
      await client.post('/services', payload)
      message.success('Service created!')
      onSuccess()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to create service')
    } finally { setAddLoading(false) }
  }

  return (
    <Form form={form} layout="vertical" onFinish={handleUpload}>
      <Form.Item name="name" label="Service Name" rules={[{ required: true }]}>
        <Input placeholder="myapp" />
      </Form.Item>
      <Form.Item name="zip_content" hidden><Input /></Form.Item>
      <Form.Item label="Select Zip File">
        <Upload.Dragger {...uploadProps}>
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">Click or drag a .zip file to this area</p>
          <p className="ant-upload-hint">Upload a zip archive containing your service files</p>
        </Upload.Dragger>
      </Form.Item>
      <Form.Item name="files" label="Or paste files as JSON">
        <Input.TextArea rows={2} placeholder='{"docker-compose.yml":"services:...", "nginx.conf":"server {...}"}' />
      </Form.Item>
      <Button type="primary" htmlType="submit" loading={addLoading} block>
        Create from Upload
      </Button>
    </Form>
  )
}

function ServiceDetailPage({ name, onBack }: { name: string; onBack: () => void }) {
  const [searchParams] = useSearchParams()
  const [service, setService] = useState<ServiceInfo | null>(null)
  const [fileContent, setFileContent] = useState('')
  const [headContent, setHeadContent] = useState('')
  const [selectedFile, setSelectedFile] = useState('')
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [gitModifiedFiles, setGitModifiedFiles] = useState<Set<string>>(new Set())
  const [gitNewFiles, setGitNewFiles] = useState<Set<string>>(new Set())
  const [treeReady, setTreeReady] = useState(false)
  const [selectedRecipe, setSelectedRecipe] = useState('')

  // Add file modal
  const [addFileOpen, setAddFileOpen] = useState(false)
  const [addFileName, setAddFileName] = useState('')
  const [addFileContent, setAddFileContent] = useState('')
  const [addFileLoading, setAddFileLoading] = useState(false)

  useEffect(() => {
    client.get(`/services/${name}`).then(r=>{setService(r.data);setTreeReady(true)}).catch(()=>message.error('Failed'))
    refreshGitStatus()
  }, [name])

  // When recipe selection changes, collapse all then expand the recipe path
  useEffect(() => {
    if (!selectedRecipe) {
      setExpandedDirs(new Set())
      return
    }
    const parts = selectedRecipe.split('/')
    const toExpand = new Set<string>()
    for (let i = 0; i < parts.length; i++) {
      toExpand.add(parts.slice(0, i + 1).join('/'))
    }
    setExpandedDirs(toExpand)
  }, [selectedRecipe])

  // Auto-load file from URL query param ?file=...
  const fileParam = searchParams.get('file')
  useEffect(() => {
    if (fileParam && service && treeReady) {
      loadFile(fileParam)
      // Auto-expand parent directories
      const parts = fileParam.split('/')
      const dirsToExpand = new Set<string>()
      for (let i = 0; i < parts.length - 1; i++) {
        dirsToExpand.add(parts.slice(0, i + 1).join('/'))
      }
      if (dirsToExpand.size > 0) {
        setExpandedDirs(prev => {
          const next = new Set(prev)
          dirsToExpand.forEach(d => next.add(d))
          return next
        })
      }
    }
  }, [fileParam, service, treeReady])

  const refreshGitStatus = async () => {
    try {
      const { data } = await client.get(`/services/${name}/git/status`)
      const modified = new Set<string>()
      const untracked = new Set<string>()
      for (const m of (data.modified || [])) { modified.add(m.file) }
      for (const u of (data.untracked || [])) { untracked.add(u.file) }
      setGitModifiedFiles(modified)
      setGitNewFiles(untracked)
    } catch { /* git not available */ }
  }

  const loadHeadContent = async (file: string): Promise<string> => {
    try {
      const { data } = await client.get(`/services/${name}/git/head-file`, { params: { file } })
      return (data.content || '').replace(/\r\n/g, '\n')
    } catch { return '' }
  }

  const loadFile = async (f: string) => {
    try {
      const [{ data: fileData }, headText] = await Promise.all([
        client.get(`/services/${name}/files/${f}`),
        loadHeadContent(f),
      ])
      setFileContent((fileData.content || '').replace(/\r\n/g, '\n'))
      setHeadContent(headText)
      setSelectedFile(f)
      setEditing(false)
    } catch { message.error('Failed to load file') }
  }

  const saveFile = async () => {
    setSaving(true)
    try {
      await client.put(`/services/${name}/files/${selectedFile}`, { content: fileContent })
      message.success('Saved')
      setHeadContent(fileContent)
      setEditing(false)
      await refreshGitStatus()
    } catch { message.error('Failed') }
    finally { setSaving(false) }
  }

  const handleCancel = () => {
    setEditing(false)
    loadFile(selectedFile)
  }

  const handleConvert = async () => {
    try {
      await client.post(`/services/${name}/convert`,{
        compose_file: service?.files.find(f=>f.includes('docker-compose')&&!f.endsWith('.j2')),
        nginx_file: service?.files.find(f=>f.includes('nginx')&&f.endsWith('.conf')&&!f.endsWith('.j2')),
        recipe_path: selectedRecipe,
      })
      message.success('Converted'); client.get(`/services/${name}`).then(r=>setService(r.data))
    } catch (err:any) { message.error(err.response?.data?.detail||'Failed') }
  }

  // Add new file
  const handleAddFile = async () => {
    if (!addFileName.trim()) { message.warning('Enter a filename'); return }
    setAddFileLoading(true)
    try {
      await client.post(`/services/${name}/files/${addFileName}`, { content: addFileContent })
      message.success(`Created ${addFileName}`)
      setAddFileOpen(false); setAddFileName(''); setAddFileContent('')
      client.get(`/services/${name}`).then(r => setService(r.data))
    } catch (err: any) { message.error(err.response?.data?.detail || 'Failed') }
    finally { setAddFileLoading(false) }
  }

  // Delete file
  const handleDeleteFile = () => {
    Modal.confirm({
      title: 'Delete File',
      content: `Permanently delete "${selectedFile}"?`,
      okText: 'Delete', okType: 'danger',
      onOk: async () => {
        try {
          await client.delete(`/services/${name}/files/${selectedFile}`)
          message.success(`Deleted ${selectedFile}`)
          setSelectedFile('')
          setFileContent('')
          client.get(`/services/${name}`).then(r => setService(r.data))
        } catch (err: any) { message.error(err.response?.data?.detail || 'Failed') }
      },
    })
  }

  const headLoaded = selectedFile && headContent !== undefined
  const hasDiff = headLoaded && fileContent !== headContent

  // ---- Build directory tree from flat file list ----
  type TreeNode = { name: string; isDir: boolean; children: Record<string, TreeNode>; hasModified: boolean; hasNew: boolean; fullPath: string }
  
  const fileTree = useMemo(() => {
    if (!service) return [] as TreeNode[]
    const filtered = service.files.filter((f: string) => {
      if (f.startsWith('.git/') || f === '.git' || f === '.gitignore' || f === '.gitattributes') return false
      if (f.startsWith('node_modules/') || f === 'node_modules') return false
      if (f.startsWith('dist/') && (f.endsWith('.js') || f.endsWith('.map') || f.endsWith('.ts'))) return false
      if (f.startsWith('.vite/') || f === '.vite') return false
      return true
    })
    
    const root: Record<string, TreeNode> = {}
    
    for (const f of filtered) {
      const parts = f.split('/')
      let current: Record<string, TreeNode> = root
      
      for (let i = 0; i < parts.length; i++) {
        const name = parts[i]
        const isLast = i === parts.length - 1
        const fullPath = parts.slice(0, i + 1).join('/')
        
        if (!current[name]) {
          current[name] = { name, isDir: !isLast, children: {}, hasModified: false, hasNew: false, fullPath }
        }
        
        if (isLast) {
          current[name].isDir = false
          current[name].hasModified = gitModifiedFiles.has(f)
          current[name].hasNew = gitNewFiles.has(f)
        }
        
        current = current[name].children
      }
    }
    
    // Convert dict-based tree to sorted array-based tree for rendering
    const dictToArray = (dict: Record<string, TreeNode>): TreeNode[] => {
      const nodes = Object.values(dict)
      for (const node of nodes) {
        if (node.isDir) {
          node.children = dictToArray(node.children) as any
          const childArr = node.children as unknown as TreeNode[]
          node.hasModified = childArr.some(c => c.hasModified)
          node.hasNew = childArr.some(c => c.hasNew)
        }
      }
      nodes.sort((a, b) => {
        if (a.isDir !== b.isDir) return a.isDir ? -1 : 1
        return a.name.localeCompare(b.name)
      })
      return nodes as any
    }
    
    return dictToArray(root)
  }, [service, gitModifiedFiles, gitNewFiles])

  // ---- Recursive file tree renderer ----
  const [expandedDirs, setExpandedDirs] = useState<Set<string>>(new Set(['app'])) // 'app' expanded by default
  
  const renderTreeNode = (node: TreeNode, depth: number): React.ReactNode => {
    const isSelected = selectedFile === node.fullPath
    const isGenerated = (service?.generated_files || []).includes(node.fullPath)
    const isNew = node.hasNew && !node.hasModified
    const isModified = node.hasModified
    const statusColor = isSelected ? '#e6f4ff' : isGenerated ? '#f6ffed' : isNew ? '#f6ffed' : isModified ? '#fff7e6' : 'transparent'
    
    if (node.isDir) {
      const expanded = expandedDirs.has(node.fullPath)
      const childArray = node.children as unknown as TreeNode[]
      return <div key={node.fullPath}>
        <div
          onClick={() => setExpandedDirs(prev => { const next = new Set(prev); if (next.has(node.fullPath)) next.delete(node.fullPath); else next.add(node.fullPath); return next })}
          style={{padding:'6px 12px',cursor:'pointer',borderRadius:4,background:statusColor,marginBottom:2,fontFamily:'monospace',fontSize:13,display:'flex',alignItems:'center',gap:6,paddingLeft:12+depth*16}}
        >
          <span>{expanded ? '📂' : '📁'}</span>
          <span style={{fontWeight:'bold'}}>{node.name}/</span>
          {node.hasNew && <Tag color="green" style={{fontSize:10,lineHeight:'16px',marginLeft:'auto'}}>N</Tag>}
          {node.hasModified && <Tag color="orange" style={{fontSize:10,lineHeight:'16px',marginLeft:'auto'}}>M</Tag>}
        </div>
        {expanded && childArray.map(c => renderTreeNode(c, depth + 1))}
      </div>
    }
    
    // File node
    return <div key={node.fullPath} onClick={() => loadFile(node.fullPath)}
      style={{padding:'6px 12px',cursor:'pointer',borderRadius:4,background:statusColor,marginBottom:2,fontFamily:'monospace',fontSize:13,display:'flex',alignItems:'center',gap:6,paddingLeft:12+depth*16}}
    >
      <span>{isGenerated?'✨':isNew?'●':'📄'}</span>
      <span style={{color:isGenerated?'#52c41a':isNew?'#52c41a':isModified?'#faad14':undefined,fontWeight:isGenerated||isNew?'bold':undefined}}>{node.name}</span>
      {isGenerated && <Tag color="green" style={{fontSize:10,lineHeight:'16px',marginLeft:'auto'}}>gen</Tag>}
      {isNew && <Tag color="green" style={{fontSize:10,lineHeight:'16px',marginLeft:'auto'}}>N</Tag>}
      {isModified && !isNew && <Tag color="orange" style={{fontSize:10,lineHeight:'16px',marginLeft:'auto'}}>M</Tag>}
    </div>
  }

  if (!service) return <Spin/>

  // Compute per-recipe status from files
  const recipePrefix = selectedRecipe ? selectedRecipe + '/' : ''
  const hasComposeTemplate = service.files.some(f => f.startsWith(recipePrefix) && f.endsWith('.yml.j2'))
  const hasNginxTemplate = service.files.some(f => f.startsWith(recipePrefix) && (f.endsWith('.nginx.conf.j2') || f.endsWith('.conf.j2')))
  const hasTemplate = hasComposeTemplate || hasNginxTemplate

  return (
    <div>
      <Button onClick={onBack} style={{marginBottom:16}}>← Back to Source Projects</Button>
      <Title level={3}>{name}</Title>
      <Card style={{marginBottom:16}}>
        <Space direction="vertical"><div><strong>Path:</strong> {service.path}</div>
        <div style={{display:'flex',alignItems:'center',gap:8,flexWrap:'wrap'}}>
          {(service.recipes && service.recipes.length > 1) && (
            <Select size="small" value={selectedRecipe} onChange={setSelectedRecipe} style={{width:200}}
              options={service.recipes.map(r => ({ value: r.path, label: r.label }))}
              placeholder="Select recipe"
            />
          )}
          <strong>Status:</strong>
          {hasComposeTemplate && <Tag color="green">Compose ✓</Tag>}
          {hasNginxTemplate && <Tag color="purple">Nginx ✓</Tag>}
          {!hasTemplate && <Button size="small" onClick={handleConvert}>Convert</Button>}
          {hasTemplate && <Button size="small" onClick={handleConvert}>Re-Convert</Button>}
        </div></Space>
      </Card>
      <Card title={<Space>Files <Button size="small" icon={<FileAddOutlined/>} onClick={() => setAddFileOpen(true)}>Add File</Button></Space>}>
        <div style={{display:'flex',gap:16}}>
          <div style={{width:280,borderRight:'1px solid #f0f0f0',paddingRight:16,overflow:'auto',maxHeight:'calc(100vh - 300px)'}}>
            <div style={{marginBottom:8,display:'flex',gap:8,alignItems:'center'}}>
              <Tag color="green">N</Tag><Text type="secondary" style={{fontSize:11}}>new</Text>
              <Tag color="orange">M</Tag><Text type="secondary" style={{fontSize:11}}>modified</Text>
            </div>
            {fileTree.map(n => renderTreeNode(n, 0))}
          </div>
          <div style={{flex:1}}>
            {selectedFile ? <div>
              <div style={{marginBottom:8,display:'flex',justifyContent:'space-between',alignItems:'center'}}>
                <Space>
                  <strong>{selectedFile}</strong>
                  {hasDiff && !editing && <Tag color="orange">Modified</Tag>}
                </Space>
                <Space>
                  {editing ? <>
                    <Button onClick={handleCancel}>Cancel</Button>
                    <Button type="primary" onClick={saveFile} loading={saving}>Save</Button>
                  </> : <>
                    <Button onClick={()=>setEditing(true)}>Edit</Button>
                    <Button danger icon={<DeleteOutlined/>} onClick={handleDeleteFile}>Delete</Button>
                  </>}
                </Space>
              </div>
              {editing ? (
                <Editor
                  height="500px"
                  width="100%"
                  defaultLanguage="yaml"
                  value={fileContent}
                  onChange={(v)=>setFileContent((v||'').replace(/\r\n/g, '\n'))}
                  theme="vs-dark"
                  options={{ minimap:{enabled:false}, fontSize:13, lineNumbers:'on', glyphMargin:true, automaticLayout:true, scrollBeyondLastLine:false, wordWrap:'on' }}
                />
              ) : hasDiff ? (
                <DiffEditor
                  height="500px"
                  width="100%"
                  language="yaml"
                  original={headContent}
                  modified={fileContent}
                  theme="vs-dark"
                  options={{
                    minimap: { enabled: false },
                    fontSize: 13,
                    lineNumbers: 'on',
                    automaticLayout: true,
                    scrollBeyondLastLine: false,
                    wordWrap: 'on',
                    readOnly: true,
                    renderSideBySide: false,
                  }}
                />
              ) : (
                <Editor
                  height="500px"
                  width="100%"
                  defaultLanguage="yaml"
                  value={fileContent}
                  theme="vs-dark"
                  options={{ minimap:{enabled:false}, fontSize:13, lineNumbers:'on', readOnly:true, automaticLayout:true, scrollBeyondLastLine:false, wordWrap:'on' }}
                />
              )}
            </div> : <Empty description="Select a file"/>}
          </div>
        </div>
      </Card>

      {/* Add File Modal */}
      <Modal
        title={`Add File to ${name}`}
        open={addFileOpen}
        onCancel={() => { setAddFileOpen(false); setAddFileName(''); setAddFileContent('') }}
        onOk={handleAddFile}
        confirmLoading={addFileLoading}
        okText="Create"
      >
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <Input
            placeholder="filename (e.g. docker-compose.yml)"
            value={addFileName}
            onChange={e => setAddFileName(e.target.value)}
            prefix={<FileAddOutlined/>}
          />
          <Editor
            height="300px"
            defaultLanguage="yaml"
            value={addFileContent}
            onChange={(v) => setAddFileContent((v || '').replace(/\r\n/g, '\n'))}
            theme="vs-dark"
            options={{ minimap: { enabled: false }, fontSize: 13, lineNumbers: 'on', automaticLayout: true, scrollBeyondLastLine: false, wordWrap: 'on' }}
          />
        </Space>
      </Modal>
    </div>
  )
}
