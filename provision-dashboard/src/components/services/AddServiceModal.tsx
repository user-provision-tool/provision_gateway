import { useState, useEffect } from 'react'
import { Modal, Tabs, Input, Button, Upload, Select, Space, message, Card, Spin } from 'antd'
import { GithubOutlined, UploadOutlined, AppstoreOutlined } from '@ant-design/icons'
import client from '../../api/client'
import * as servicesApi from '../../api/services'

interface TemplateOption {
  id: number; name: string; description?: string; category?: string; icon?: string
}

interface AddServiceModalProps {
  open: boolean
  onClose: () => void
  onCreated: () => void
}

export default function AddServiceModal({ open, onClose, onCreated }: AddServiceModalProps) {
  const [mode, setMode] = useState<'git' | 'upload' | 'template'>('git')
  const [loading, setLoading] = useState(false)

  // Git mode state
  const [repoUrl, setRepoUrl] = useState('')
  const [branch, setBranch] = useState('main')
  const [gitName, setGitName] = useState('')
  const [useProxy, setUseProxy] = useState(false)

  // Upload mode state
  const [uploadName, setUploadName] = useState('')
  const [files, setFiles] = useState<any[]>([])
  const [filesBase64, setFilesBase64] = useState<Record<string, string>>({})

  // Template mode state
  const [templates, setTemplates] = useState<TemplateOption[]>([])
  const [loadingTemplates, setLoadingTemplates] = useState(false)
  const [selectedTemplateId, setSelectedTemplateId] = useState<number | null>(null)
  const [templateName, setTemplateName] = useState('')

  useEffect(() => {
    if (open && mode === 'template') {
      loadTemplates()
    }
  }, [open, mode])

  const loadTemplates = async () => {
    setLoadingTemplates(true)
    try {
      const { data } = await client.get('/services/templates')
      setTemplates(data.templates || [])
    } catch {
      setTemplates([])
    } finally {
      setLoadingTemplates(false)
    }
  }

  const handleCreate = async () => {
    setLoading(true)
    try {
      if (mode === 'git') {
        await servicesApi.createServiceGit({ mode: 'git', repo_url: repoUrl, branch, name: gitName, use_proxy: useProxy })
      } else if (mode === 'upload') {
        // Read uploaded files and send as base64 content in JSON
        const fileContents: Record<string, string> = { ...filesBase64 }
        for (const file of files) {
          const b64 = await new Promise<string>((resolve, reject) => {
            const reader = new FileReader()
            reader.onload = () => {
              const result = reader.result as string
              const data = result.includes('base64,') ? result.split('base64,')[1] : result
              resolve(data)
            }
            reader.onerror = () => reject(reader.error)
            reader.readAsDataURL(file)
          })
          fileContents[file.name] = b64
        }
        await servicesApi.createServiceGit({ mode: 'upload', name: uploadName, files: fileContents })
      } else if (mode === 'template') {
        if (!selectedTemplateId) {
          message.warning('Please select a template')
          setLoading(false)
          return
        }
        if (!templateName) {
          message.warning('Please enter a project name')
          setLoading(false)
          return
        }
        await servicesApi.createServiceGit({
          mode: 'template',
          name: templateName,
          template_id: selectedTemplateId,
        })
      }
      message.success('Service created')
      onCreated()
      onClose()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to create service')
    } finally { setLoading(false) }
  }

  const tabItems = [
    {
      key: 'git',
      label: <span><GithubOutlined /> Git Repository</span>,
      children: (
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input placeholder="https://github.com/user/repo.git" value={repoUrl} onChange={e => setRepoUrl(e.target.value)} />
          <Input placeholder="Branch (default: main)" value={branch} onChange={e => setBranch(e.target.value)} />
          <Input placeholder="Project name" value={gitName} onChange={e => setGitName(e.target.value)} />
        </Space>
      ),
    },
    {
      key: 'upload',
      label: <span><UploadOutlined /> Upload Files</span>,
      children: (
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input placeholder="Project name" value={uploadName} onChange={e => setUploadName(e.target.value)} />
          <Upload.Dragger multiple beforeUpload={(file) => { setFiles(prev => [...prev, file]); return false }} showUploadList>
            <p><UploadOutlined style={{ fontSize: 24 }} /></p>
            <p>Drop files here or click to browse</p>
          </Upload.Dragger>
        </Space>
      ),
    },
    {
      key: 'template',
      label: <span><AppstoreOutlined /> From Template</span>,
      children: (
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input placeholder="Project name" value={templateName} onChange={e => setTemplateName(e.target.value)} />
          {loadingTemplates ? <Spin /> : templates.length === 0 ? (
            <Card size="small"><em>No templates available. Built-in templates will appear here.</em></Card>
          ) : (
            <Select
              style={{ width: '100%' }}
              placeholder="Select a template"
              value={selectedTemplateId}
              onChange={(val) => setSelectedTemplateId(val)}
              options={templates.map(t => ({
                value: t.id,
                label: `${t.name}${t.description ? ' — ' + t.description : ''}`,
              }))}
            />
          )}
        </Space>
      ),
    },
  ]

  return (
    <Modal title="Add New Service" open={open} onCancel={onClose} onOk={handleCreate} confirmLoading={loading} width={600}>
      <Tabs activeKey={mode} onChange={k => { setMode(k as any); if (k === 'template') loadTemplates() }} items={tabItems} />
    </Modal>
  )
}
