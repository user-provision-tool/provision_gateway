import { useState } from 'react'
import { Modal, Tabs, Input, Button, Upload, Select, Space, message } from 'antd'
import { GithubOutlined, UploadOutlined } from '@ant-design/icons'
import * as servicesApi from '../../api/services'

interface AddServiceModalProps {
  open: boolean
  onClose: () => void
  onCreated: () => void
}

export default function AddServiceModal({ open, onClose, onCreated }: AddServiceModalProps) {
  const [mode, setMode] = useState<'git' | 'upload'>('git')
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
  ]

  return (
    <Modal title="Add New Service" open={open} onCancel={onClose} onOk={handleCreate} confirmLoading={loading} width={600}>
      <Tabs activeKey={mode} onChange={k => setMode(k as any)} items={tabItems} />
    </Modal>
  )
}
