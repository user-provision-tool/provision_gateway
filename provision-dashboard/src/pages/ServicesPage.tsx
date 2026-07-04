import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  Typography, Card, Table, Button, Modal, Form, Input,
  Space, message, Tag, Empty, Tabs, Spin
} from 'antd'
import {
  PlusOutlined, DeleteOutlined, FolderOpenOutlined,
  GithubOutlined, UploadOutlined, FileTextOutlined
} from '@ant-design/icons'
import client from '../api/client'

const { Title } = Typography

interface ServiceInfo {
  name: string; path: string; files: string[]
  has_compose_template: boolean; has_nginx_template: boolean
  has_dockerfile: boolean; active_users: number
  active_instances: string[]; created_at: string
}

export default function ServicesPage() {
  const { name } = useParams<{ name?: string }>()
  const navigate = useNavigate()
  const [services, setServices] = useState<ServiceInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [addModalOpen, setAddModalOpen] = useState(false)
  const [addLoading, setAddLoading] = useState(false)
  const [addMode, setAddMode] = useState<'git' | 'upload' | 'template'>('git')
  const [form] = Form.useForm()

  useEffect(() => { loadServices() }, [])

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
    { title: 'Files', dataIndex: 'files', key: 'files',
      render: (files: string[]) => <Space size={4} wrap>{files.slice(0,5).map(f=><Tag key={f} color="blue">{f}</Tag>)}{files.length>5&&<Tag>+{files.length-5}</Tag>}</Space> },
    { title: 'Templates', key: 'templates',
      render: (_:any, r:ServiceInfo) => <Space>{r.has_compose_template&&<Tag color="green">Compose</Tag>}{r.has_nginx_template&&<Tag color="purple">Nginx</Tag>}{r.has_dockerfile&&<Tag color="orange">Dockerfile</Tag>}</Space> },
    { title: 'Actions', key: 'actions',
      render: (_:any, r:ServiceInfo) => <Button type="text" danger icon={<DeleteOutlined/>} onClick={()=>handleDelete(r.name)}/> },
  ]

  return (
    <div>
      <div style={{display:'flex',justifyContent:'space-between',marginBottom:16}}>
        <Title level={3} style={{margin:0}}>Source Projects</Title>
        <Button type="primary" icon={<PlusOutlined/>} onClick={()=>setAddModalOpen(true)}>Add Project</Button>
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
              <Button type="primary" htmlType="submit" loading={addLoading} block>Clone & Create</Button>
            </Form> },
          { key:'upload', label:<span><UploadOutlined/> Upload Files</span>, children:
            <Form form={form} layout="vertical" onFinish={handleAdd}>
              <Form.Item name="name" label="Service Name" rules={[{required:true}]}><Input placeholder="myapp"/></Form.Item>
              <Form.Item name="files" label="Files (JSON)"><Input.TextArea rows={6} placeholder='{"docker-compose.yml":"services:...","nginx.conf":"server {...}"}'/></Form.Item>
              <Button type="primary" htmlType="submit" loading={addLoading} block>Create Service</Button>
            </Form> },
          { key:'template', label:<span><FileTextOutlined/> From Template</span>, children:<Empty description="Coming soon."/> },
        ]}/>
      </Modal>
    </div>
  )
}

function ServiceDetailPage({ name, onBack }: { name: string; onBack: () => void }) {
  const [service, setService] = useState<ServiceInfo | null>(null)
  const [fileContent, setFileContent] = useState('')
  const [selectedFile, setSelectedFile] = useState('')
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => { client.get(`/services/${name}`).then(r=>setService(r.data)).catch(()=>message.error('Failed')) }, [name])

  const loadFile = async (f: string) => {
    try { const {data}=await client.get(`/services/${name}/files/${f}`); setFileContent(data.content); setSelectedFile(f); setEditing(false) }
    catch { message.error('Failed to load') }
  }
  const saveFile = async () => {
    setSaving(true)
    try { await client.put(`/services/${name}/files/${selectedFile}`,{content:fileContent}); message.success('Saved'); setEditing(false) }
    catch { message.error('Failed') }
    finally { setSaving(false) }
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
          <div style={{width:250,borderRight:'1px solid #f0f0f0',paddingRight:16}}>
            {service.files.map((f:string)=><div key={f} onClick={()=>loadFile(f)} style={{padding:'8px 12px',cursor:'pointer',borderRadius:4,background:selectedFile===f?'#e6f4ff':'transparent',marginBottom:4,fontFamily:'monospace',fontSize:13}}>📄 {f}</div>)}
          </div>
          <div style={{flex:1}}>
            {selectedFile ? <div>
              <div style={{marginBottom:8,display:'flex',justifyContent:'space-between'}}>
                <strong>{selectedFile}</strong>
                <Space>{editing?<><Button onClick={()=>{setEditing(false);loadFile(selectedFile)}}>Cancel</Button><Button type="primary" onClick={saveFile} loading={saving}>Save</Button></>:<Button onClick={()=>setEditing(true)}>Edit</Button>}</Space>
              </div>
              {editing?<Input.TextArea value={fileContent} onChange={e=>setFileContent(e.target.value)} rows={20} style={{fontFamily:'monospace',fontSize:13}}/>:
              <pre style={{background:'#f5f5f5',padding:16,borderRadius:8,overflow:'auto',maxHeight:500,fontSize:13,fontFamily:'monospace'}}>{fileContent}</pre>}
            </div> : <Empty description="Select a file"/>}
          </div>
        </div>
      </Card>
    </div>
  )
}
