import { useState, useEffect, useMemo } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import {
  Typography, Card, Table, Button, Modal, Form, Input,
  Space, message, Tag, Empty, Tabs, Spin, Checkbox
} from 'antd'
import {
  PlusOutlined, DeleteOutlined, FolderOpenOutlined,
  GithubOutlined, UploadOutlined, FileTextOutlined
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
  const [addMode, setAddMode] = useState<'git' | 'upload' | 'template'>('git')
  const [form] = Form.useForm()
  const [proxyEnabled, setProxyEnabled] = useState(false)

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
      setServices(data.services || [])
    } catch (err: any) {
      message.error('Failed to load services')
    } finally { setLoading(false) }
  }

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

  if (name) return <ServiceDetailPage name={name} onBack={() => navigate('/services')} />

  const columns = [
    { title: 'Name', dataIndex: 'name', key: 'name',
      render: (t: string) => <Button type="link" onClick={() => navigate(`/services/${t}`)}><FolderOpenOutlined /> {t}</Button> },
    { title: 'Templates', key: 'templates',
      render: (_:any, r:ServiceInfo) => {
        // First identify generated files
        const generatedSet = new Set<string>()
        r.files.forEach((f: string) => {
          // Only system-generated compose/nginx files are "generated" — not .env files
          if (f.match(/docker-compose\.user-.*\.yml$/) || f.match(/\.user-.*\.nginx\.conf$/)) {
            generatedSet.add(f)
          }
        });
        (r.generated_files || []).forEach((f: string) => generatedSet.add(f))
        
        // Templates = non-generated files that are templates (.j2, Dockerfile, .env, etc.)
        const temps: string[] = []
        r.files.forEach((f: string) => {
          if (generatedSet.has(f)) return // skip generated files
          if (f.endsWith('.yml.j2') || f.endsWith('.conf.j2') || f.includes('Dockerfile') || f.startsWith('.env')) {
            temps.push(f)
          }
        })
        return <Space size={4} wrap>{temps.length>0 ? temps.map(f=><Tag key={f} color="green" style={{cursor:'pointer'}} onClick={()=>navigate(`/services/${r.name}?file=${f}`)}>{f}</Tag>) : <Tag>none</Tag>}</Space>
      }
    },
    { title: 'Generated Files', key: 'generated',
      render: (_:any, r:ServiceInfo) => {
        const gens: string[] = []
        r.files.forEach((f: string) => {
          // System-generated per-user files: docker-compose.user-*.yml, *.user-*.nginx.conf
          if (f.match(/docker-compose\.user-.*\.yml$/) || f.match(/\.user-.*\.nginx\.conf$/)) {
            gens.push(f)
          }
        })
        const backendGens = r.generated_files || []
        const allGens = [...new Set([...gens, ...backendGens])]
        return <Space size={4} wrap>{allGens.length>0 ? allGens.map(f=><Tag key={f} color="gold" style={{cursor:'pointer'}} onClick={()=>navigate(`/services/${r.name}?file=${f}`)}>{f}</Tag>) : <Tag>none</Tag>}</Space>
      }
    },
    { title: 'Actions', key: 'actions',
      render: (_:any, r:ServiceInfo) => <Space>
        <Button size="small" type="primary" onClick={()=>navigate(`/services/${r.name}`)}>Deploy</Button>
        {isAdmin && <Button size="small" danger icon={<DeleteOutlined/>} onClick={()=>handleDelete(r.name)}/>}
      </Space> },
  ]

  return (
    <div>
      <div style={{display:'flex',justifyContent:'space-between',marginBottom:16}}>
        <Title level={3} style={{margin:0}}>Source Projects</Title>
        {isAdmin && <Button type="primary" icon={<PlusOutlined/>} onClick={()=>setAddModalOpen(true)}>Add Project</Button>}
      </div>
      <Card>
        {loading ? <Spin/> : services.length===0 ? <Empty description="No source projects yet"><Button type="primary" icon={<PlusOutlined/>} onClick={()=>setAddModalOpen(true)}>Add Project</Button></Empty> :
        <Table dataSource={services} columns={columns} rowKey="name" pagination={false}/>}
      </Card>
      <Modal title="Add Source Project" open={addModalOpen} onCancel={()=>{setAddModalOpen(false);form.resetFields()}} footer={null} width={560}>
        <Tabs activeKey={addMode} onChange={(k)=>setAddMode(k as any)} items={[
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
            <Form form={form} layout="vertical" onFinish={handleAdd}>
              <Form.Item name="name" label="Service Name" rules={[{required:true}]}><Input placeholder="myapp"/></Form.Item>
              <Form.Item name="zip_content" label="Zip (base64)" help="Paste base64-encoded zip"><Input.TextArea rows={3} placeholder="UEsDBBQAAAAI..."/></Form.Item>
              <Form.Item name="files" label="Or files as JSON"><Input.TextArea rows={2} placeholder='{"file":"content"}'/></Form.Item>
              <Button type="primary" htmlType="submit" loading={addLoading} block>Create from Upload</Button>
            </Form> },
          { key:'template', label:<span><FileTextOutlined/> From Template</span>, children:
            <Form form={form} layout="vertical" onFinish={async (values)=>{
              setAddLoading(true)
              try {
                const { data } = await client.post('/llm/generate', {
                  prompt: `Create a production-ready ${values.template_type||'web app'} service named "${values.name}". Generate docker-compose.yml, nginx.conf, .env, and Dockerfile.`,
                  generate_type: 'service_config'
                })
                // Save generated files
                await client.post('/services/save-generated', { name: values.name, files: data.files||data })
                message.success('Service created from template!')
                setAddModalOpen(false); form.resetFields(); loadServices()
              } catch (err: any) { message.error(err.response?.data?.detail || 'Generation failed') }
              finally { setAddLoading(false) }
            }}>
              <Form.Item name="name" label="Service Name" rules={[{required:true}]}><Input placeholder="myapp"/></Form.Item>
              <Form.Item name="template_type" label="Service Type" initialValue="web app">
                <Input placeholder="e.g. Python FastAPI + Redis, WordPress, Node.js app"/>
              </Form.Item>
              <Form.Item name="use_proxy" valuePropName="checked">
                <Checkbox disabled={!proxyEnabled}>
                  Use global proxy (for LLM API)
                  {!proxyEnabled && <span style={{color:'#999',fontSize:12}}> (enable in Settings)</span>}
                </Checkbox>
              </Form.Item>
              <Button type="primary" htmlType="submit" loading={addLoading} block icon={<FileTextOutlined/>}>Generate from Template</Button>
            </Form> },
        ]}/>
      </Modal>
    </div>
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

  useEffect(() => {
    client.get(`/services/${name}`).then(r=>{setService(r.data);setTreeReady(true)}).catch(()=>message.error('Failed'))
    refreshGitStatus()
  }, [name])

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
      })
      message.success('Converted'); client.get(`/services/${name}`).then(r=>setService(r.data))
    } catch (err:any) { message.error(err.response?.data?.detail||'Failed') }
  }

  const headLoaded = selectedFile && headContent !== undefined
  const hasDiff = headLoaded && fileContent !== headContent

  // ---- Build directory tree from flat file list ----
  type TreeNode = { name: string; isDir: boolean; children: TreeNode[]; hasModified: boolean; hasNew: boolean; fullPath: string }
  
  const fileTree = useMemo(() => {
    if (!service) return []
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
      let current = root
      
      for (let i = 0; i < parts.length; i++) {
        const name = parts[i]
        const isLast = i === parts.length - 1
        const fullPath = parts.slice(0, i + 1).join('/')
        
        if (!current[name]) {
          current[name] = { name, isDir: !isLast, children: [], hasModified: false, hasNew: false, fullPath }
          // Link to parent's children array for rendering
          if (i === 0) {
            // top-level entry — will be added to result later
          }
        }
        
        if (isLast) {
          current[name].isDir = false
          current[name].hasModified = gitModifiedFiles.has(f)
          current[name].hasNew = gitNewFiles.has(f)
        }
        
        current = current[name].children as any
      }
    }
    
    // Convert root map to sorted array
    const result: TreeNode[] = Object.values(root)
    // Compute directory-level status (post-order)
    const computeDirStatus = (nodes: TreeNode[]) => {
      for (const node of nodes) {
        if (node.isDir) {
          computeDirStatus(node.children)
          node.hasModified = node.children.some(c => c.hasModified)
          node.hasNew = node.children.some(c => c.hasNew)
        }
      }
    }
    computeDirStatus(result)
    
    // Sort: dirs first, then files; alphabetical within each group
    const sortNodes = (nodes: TreeNode[]) => {
      nodes.sort((a, b) => {
        if (a.isDir !== b.isDir) return a.isDir ? -1 : 1
        return a.name.localeCompare(b.name)
      })
      for (const n of nodes) { if (n.isDir) sortNodes(n.children) }
    }
    sortNodes(result)
    
    return result
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
        {expanded && node.children.map(c => renderTreeNode(c, depth + 1))}
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
  return (
    <div>
      <Button onClick={onBack} style={{marginBottom:16}}>← Back to Source Projects</Button>
      <Title level={3}>{name}</Title>
      <Card style={{marginBottom:16}}>
        <Space direction="vertical"><div><strong>Path:</strong> {service.path}</div>
        <div><strong>Status:</strong> {service.has_compose_template&&<Tag color="green">Compose ✓</Tag>}{service.has_nginx_template&&<Tag color="purple">Nginx ✓</Tag>}
        {!service.has_compose_template&&!service.has_nginx_template&&<Button size="small" onClick={handleConvert}>Convert</Button>}</div></Space>
      </Card>
      <Card title="Files">
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
                  </> : <Button onClick={()=>setEditing(true)}>Edit</Button>}
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
    </div>
  )
}
