import { useState } from 'react'
import { Modal, Tabs, Input, Button, Upload, Select, Space, message } from 'antd'
import { GithubOutlined, UploadOutlined, FileOutlined } from '@ant-design/icons'
import * as servicesApi from '../../api/services'

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

  // Template mode state
  const [templateId, setTemplateId] = useState<number | null>(null)
  const [templateName, setTemplateName] = useState('')

  const handleCreate = async () => {
    setLoading(true)
    try {
      if (mode === 'git') {
        await servicesApi.createServiceGit({ mode: 'git', repo_url: repoUrl, branch, name: gitName, use_proxy: useProxy })
      } else if (mode === 'upload') {
        const formData = new FormData()
        formData.append('mode', 'upload')
        formData.append('name', uploadName)
        files.forEach(f => formData.append('files', f))
        await servicesApi.createServiceGit(formData)
      } else if (mode === 'template' && templateId) {
        await servicesApi.createServiceGit({ mode: 'template', template_id: templateId, name: templateName })
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
      label: <span><FileOutlined /> Template</span>,
      children: (
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input type="number" placeholder="Template ID" value={templateId || ''} onChange={e => setTemplateId(Number(e.target.value) || null)} />
          <Input placeholder="Project name" value={templateName} onChange={e => setTemplateName(e.target.value)} />
        </Space>
      ),
    },
  ]

  return (
    <Modal title="Add New Service" open={open} onCancel={onClose} onOk={handleCreate} confirmLoading={loading} width={600}>
      <Tabs activeKey={mode} onChange={k => setMode(k as any)} items={tabItems} />
    </Modal>
  )
}
