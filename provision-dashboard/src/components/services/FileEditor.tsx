import { useState } from 'react'
import { Drawer, Space, Tag, Typography, Button, Spin, Modal, message } from 'antd'
import { SaveOutlined, UndoOutlined } from '@ant-design/icons'
import Editor from '@monaco-editor/react'
import client from '../../api/client'

const { Text } = Typography

interface FileEditorProps {
  open: boolean
  file: { user: string; service: string; label: string; fileType: string; filename: string; path: string } | null
  onClose: () => void
  getLanguage?: (fileType: string) => string
}

const defaultGetLanguage = (fileType: string) => {
  switch (fileType) {
    case 'compose': return 'yaml'
    case 'nginx': return 'nginx'
    case 'env': return 'shell'
    default: return 'plaintext'
  }
}

export default function FileEditor({ open, file, onClose, getLanguage = defaultGetLanguage }: FileEditorProps) {
  const [content, setContent] = useState('')
  const [original, setOriginal] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  const loadFile = async (user: string, service: string, label: string, fileType: string) => {
    setLoading(true)
    try {
      const { data } = await client.get(`/users/${user}/${service}/${label}/deployment-files/${fileType}`)
      setContent(data.content || '')
      setOriginal(data.content || '')
    } catch (err: any) {
      if (err.response?.status === 404) {
        setContent('')
        setOriginal('')
        message.info('File does not exist yet. Create it by saving.')
      } else {
        message.error('Failed to load file')
        onClose()
      }
    } finally { setLoading(false) }
  }

  // Load when file changes
  if (file && open) {
    loadFile(file.user, file.service, file.label, file.fileType)
  }

  const handleSave = async () => {
    if (!file) return
    setSaving(true)
    try {
      await client.put(`/users/${file.user}/${file.service}/${file.label}/deployment-files/${file.fileType}`, { content })
      message.success('File saved')
      setOriginal(content)
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to save')
    } finally { setSaving(false) }
  }

  const handleClose = () => {
    if (content !== original) {
      Modal.confirm({
        title: 'Unsaved changes',
        content: 'Discard changes?',
        onOk: onClose,
      })
    } else { onClose() }
  }

  const isModified = content !== original

  return (
    <Drawer
      title={file ? <Space><Text strong>{file.filename}</Text><Tag>{file.fileType}</Tag><Text type="secondary">for {file.user}/{file.service}/{file.label}</Text></Space> : 'File Editor'}
      open={open}
      onClose={handleClose}
      width="70%"
      extra={
        <Space>
          {isModified && <Tag color="orange">Modified</Tag>}
          <Button icon={<UndoOutlined />} onClick={() => setContent(original)} disabled={!isModified}>Reset</Button>
          <Button type="primary" icon={<SaveOutlined />} onClick={handleSave} loading={saving} disabled={!isModified}>Save & Close</Button>
        </Space>
      }
    >
      {loading ? <Spin /> : (
        <div style={{ height: 'calc(100vh - 180px)' }}>
          <Editor
            language={getLanguage(file?.fileType || 'plaintext')}
            value={content}
            onChange={v => setContent(v || '')}
            theme="vs-dark"
            options={{ minimap: { enabled: false }, wordWrap: 'on', automaticLayout: true }}
          />
        </div>
      )}
    </Drawer>
  )
}
