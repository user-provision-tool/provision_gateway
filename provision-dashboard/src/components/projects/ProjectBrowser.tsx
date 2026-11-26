import { useEffect, useRef, useState } from 'react'
import { Button, Empty, Popconfirm, Space, Spin, Tag, Typography, message } from 'antd'
import { CheckOutlined, DeleteOutlined } from '@ant-design/icons'
import Editor, { DiffEditor } from '@monaco-editor/react'
import client from '../../api/client'

const { Text } = Typography

type TreeNode = {
  name: string
  path: string
  type: 'file' | 'dir'
  is_generated: boolean
  is_template: boolean
}

interface Props {
  serviceName: string
  /** A project-root-relative file to open on mount (e.g. ?file=...). */
  initialFile?: string
  /** CSS height of the whole browser; the editor fills the remaining space. */
  height?: string | number
  /** C16 — adds the currently selected file to the shared per-service context. */
  onAddToContext?: (path: string) => void
  /** Files already in the context; the button shows a check when selected. */
  contextFiles?: string[]
}

/** C16 — the one project tree + preview/editor, used by the Source Projects
 *  page and embedded verbatim in the Additional-context popup. A fixed height
 *  is never used: the list scrolls in its column and the editor fills the pane. */
export default function ProjectBrowser({
  serviceName, initialFile, height = '100%', onAddToContext, contextFiles = [],
}: Props) {
  const [treeNodes, setTreeNodes] = useState<Record<string, TreeNode[]>>({})
  const [loadingDirs, setLoadingDirs] = useState<Set<string>>(new Set())
  const [expandedDirs, setExpandedDirs] = useState<Set<string>>(new Set())
  const [selectedFile, setSelectedFile] = useState('')
  const [fileContent, setFileContent] = useState('')
  const [headContent, setHeadContent] = useState('')
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [gitModifiedFiles, setGitModifiedFiles] = useState<Set<string>>(new Set())
  const [gitNewFiles, setGitNewFiles] = useState<Set<string>>(new Set())
  const openedFileRef = useRef(false)

  const loadDir = async (dir: string) => {
    setLoadingDirs(prev => { const n = new Set(prev); n.add(dir); return n })
    try {
      const { data } = await client.get(`/projects/${serviceName}/tree`, { params: dir ? { dir } : {} })
      setTreeNodes(prev => ({ ...prev, [dir]: data.children || [] }))
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to load directory')
    } finally {
      setLoadingDirs(prev => { const n = new Set(prev); n.delete(dir); return n })
    }
  }

  const refreshGitStatus = async () => {
    try {
      const { data } = await client.get(`/projects/${serviceName}/git/status`)
      setGitModifiedFiles(new Set((data.modified || []).map((m: any) => m.file)))
      setGitNewFiles(new Set((data.untracked || []).map((u: any) => u.file)))
    } catch { /* git not available */ }
  }

  const loadHeadContent = async (file: string): Promise<string> => {
    try {
      const { data } = await client.get(`/projects/${serviceName}/git/head-file`, { params: { file } })
      return (data.content || '').replace(/\r\n/g, '\n')
    } catch { return '' }
  }

  const loadFile = async (f: string) => {
    try {
      const [{ data: fileData }, headText] = await Promise.all([
        client.get(`/projects/${serviceName}/files/${f}`),
        loadHeadContent(f),
      ])
      setFileContent((fileData.content || '').replace(/\r\n/g, '\n'))
      setHeadContent(headText)
      setSelectedFile(f)
      setEditing(false)
    } catch { message.error('Failed to load file') }
  }

  const saveFile = async () => {
    if (!selectedFile) return
    setSaving(true)
    try {
      await client.put(`/projects/${serviceName}/files/${selectedFile}`, { content: fileContent })
      setHeadContent(fileContent)
      setEditing(false)
      message.success('Saved')
      refreshGitStatus()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to save file')
    } finally {
      setSaving(false)
    }
  }

  const deleteFile = async () => {
    if (!selectedFile) return
    try {
      await client.delete(`/projects/${serviceName}/files/${selectedFile}`)
      message.success(`Deleted ${selectedFile}`)
      setSelectedFile('')
      setFileContent('')
      setHeadContent('')
      setTreeNodes({})
      setExpandedDirs(new Set())
      loadDir('')
      refreshGitStatus()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to delete file')
    }
  }

  useEffect(() => {
    setTreeNodes({})
    setExpandedDirs(new Set())
    setSelectedFile('')
    setFileContent('')
    setHeadContent('')
    openedFileRef.current = false
    loadDir('')
    refreshGitStatus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serviceName])

  useEffect(() => {
    if (!initialFile || openedFileRef.current) return
    openedFileRef.current = true
    loadFile(initialFile)
    const parts = initialFile.split('/')
    const dirsToExpand = new Set<string>()
    for (let i = 0; i < parts.length - 1; i++) {
      const d = parts.slice(0, i + 1).join('/')
      dirsToExpand.add(d)
      loadDir(d)
    }
    if (dirsToExpand.size > 0) {
      setExpandedDirs(prev => { const next = new Set(prev); dirsToExpand.forEach(d => next.add(d)); return next })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialFile, serviceName])

  const toggleDir = (node: TreeNode) => {
    setExpandedDirs(prev => {
      const next = new Set(prev)
      if (next.has(node.path)) next.delete(node.path)
      else next.add(node.path)
      return next
    })
    if (!treeNodes[node.path]) loadDir(node.path)
  }

  const renderDirChildren = (dir: string, depth: number): React.ReactNode => {
    const nodes = treeNodes[dir] || []
    return <div key={dir || '__root__'}>
      {nodes.map(n => {
        const fullPath = n.path
        const isSelected = selectedFile === fullPath
        const isNew = gitNewFiles.has(fullPath) && !gitModifiedFiles.has(fullPath)
        const isModified = gitModifiedFiles.has(fullPath)
        const isGenerated = n.is_generated
        const statusColor = isSelected ? '#e6f4ff' : isGenerated ? '#f6ffed' : isNew ? '#f6ffed' : isModified ? '#fff7e6' : 'transparent'
        if (n.type === 'dir') {
          const expanded = expandedDirs.has(fullPath)
          return <div key={fullPath}>
            <div onClick={() => toggleDir(n)}
              style={{ padding: '6px 12px', cursor: 'pointer', borderRadius: 4, background: statusColor, marginBottom: 2, fontFamily: 'monospace', fontSize: 13, display: 'flex', alignItems: 'center', gap: 6, paddingLeft: 12 + depth * 16 }}
            >
              <span>{expanded ? '📂' : '📁'}</span>
              <span style={{ fontWeight: 'bold' }}>{n.name}/</span>
              {isNew && <Tag color="green" style={{ fontSize: 10, lineHeight: '16px', marginLeft: 'auto' }}>N</Tag>}
              {isModified && <Tag color="orange" style={{ fontSize: 10, lineHeight: '16px', marginLeft: 'auto' }}>M</Tag>}
            </div>
            {expanded && renderDirChildren(fullPath, depth + 1)}
          </div>
        }
        return <div key={fullPath} onClick={() => loadFile(fullPath)}
          style={{ padding: '6px 12px', cursor: 'pointer', borderRadius: 4, background: statusColor, marginBottom: 2, fontFamily: 'monospace', fontSize: 13, display: 'flex', alignItems: 'center', gap: 6, paddingLeft: 12 + depth * 16 }}
        >
          <span>{isGenerated ? '✨' : isNew ? '●' : '📄'}</span>
          <span style={{ color: isGenerated ? '#52c41a' : isNew ? '#52c41a' : isModified ? '#faad14' : undefined, fontWeight: isGenerated || isNew ? 'bold' : undefined }}>{n.name}</span>
          {isGenerated && <Tag color="green" style={{ fontSize: 10, lineHeight: '16px', marginLeft: 'auto' }}>gen</Tag>}
          {isNew && <Tag color="green" style={{ fontSize: 10, lineHeight: '16px', marginLeft: 'auto' }}>N</Tag>}
          {isModified && !isNew && <Tag color="orange" style={{ fontSize: 10, lineHeight: '16px', marginLeft: 'auto' }}>M</Tag>}
        </div>
      })}
      {loadingDirs.has(dir) && <div style={{ padding: '8px 12px' }}><Spin size="small" /></div>}
    </div>
  }

  const headLoaded = selectedFile && headContent !== ''
  const hasDiff = headLoaded && fileContent !== headContent
  const inContext = !!selectedFile && contextFiles.includes(selectedFile)

  return (
    <div style={{ display: 'flex', gap: 16, height, minHeight: 0 }}>
      <div style={{ width: 280, flex: '0 0 280px', borderRight: '1px solid #f0f0f0', paddingRight: 16, overflow: 'auto', minHeight: 0 }}>
        <div style={{ marginBottom: 8, display: 'flex', gap: 8, alignItems: 'center' }}>
          <Tag color="green">N</Tag><Text type="secondary" style={{ fontSize: 11 }}>new</Text>
          <Tag color="orange">M</Tag><Text type="secondary" style={{ fontSize: 11 }}>modified</Text>
        </div>
        {renderDirChildren('', 0)}
      </div>
      <div style={{ flex: 1, minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        {selectedFile ? <>
          <div style={{ marginBottom: 8, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
            <Space>
              <strong>{selectedFile}</strong>
              {hasDiff && !editing && <Tag color="orange">Modified</Tag>}
            </Space>
            <Space>
              {onAddToContext && (
                <Button
                  size="small"
                  icon={inContext ? <CheckOutlined /> : undefined}
                  disabled={inContext}
                  onClick={() => onAddToContext(selectedFile)}
                >
                  {inContext ? 'in context' : 'add to context'}
                </Button>
              )}
              {editing ? <>
                <Button onClick={() => { setEditing(false); loadFile(selectedFile) }}>Cancel</Button>
                <Button type="primary" onClick={saveFile} loading={saving}>Save</Button>
              </> : <>
                <Button onClick={() => setEditing(true)}>Edit</Button>
                <Popconfirm title={`Permanently delete "${selectedFile}"?`} onConfirm={deleteFile} okText="Delete" okButtonProps={{ danger: true }}>
                  <Button danger icon={<DeleteOutlined />}>Delete</Button>
                </Popconfirm>
              </>}
            </Space>
          </div>
          <div style={{ flex: 1, minHeight: 0 }}>
            {editing ? (
              <Editor
                height="100%"
                defaultLanguage="yaml"
                value={fileContent}
                onChange={(v) => setFileContent((v || '').replace(/\r\n/g, '\n'))}
                theme="vs-dark"
                options={{ minimap: { enabled: false }, fontSize: 13, lineNumbers: 'on', glyphMargin: true, automaticLayout: true, scrollBeyondLastLine: false, wordWrap: 'on' }}
              />
            ) : hasDiff ? (
              <DiffEditor
                height="100%"
                language="yaml"
                original={headContent}
                modified={fileContent}
                theme="vs-dark"
                options={{ minimap: { enabled: false }, fontSize: 13, lineNumbers: 'on', automaticLayout: true, scrollBeyondLastLine: false, wordWrap: 'on', readOnly: true, renderSideBySide: false }}
              />
            ) : (
              <Editor
                height="100%"
                defaultLanguage="yaml"
                value={fileContent}
                theme="vs-dark"
                options={{ minimap: { enabled: false }, fontSize: 13, lineNumbers: 'on', readOnly: true, automaticLayout: true, scrollBeyondLastLine: false, wordWrap: 'on' }}
              />
            )}
          </div>
        </> : <Empty description="Select a file" />}
      </div>
    </div>
  )
}
