import { useEffect, useState } from 'react'
import { Button, Input, Modal, Select, Space, Tag, Typography, message } from 'antd'
import client from '../../api/client'
import { SelectedFileRow } from './FileSetSelection'

const { Text } = Typography

export interface AdditionalContext {
  files: string[]
  links: string[]
}

export const EMPTY_CONTEXT: AdditionalContext = { files: [], links: [] }

interface Props {
  open: boolean
  onClose: () => void
  serviceName: string
  recipePath?: string
  files: string[]
  links: string[]
  onChange: (context: AdditionalContext) => void
  onOpenFile: (name: string) => void
}

/** R3 — the existing project browser (the tree endpoint) plus a URL input.
 *  Both are generation context only: never sent to the deployment. */
export default function AdditionalContextPopup({
  open, onClose, serviceName, recipePath, files, links, onChange, onOpenFile,
}: Props) {
  const [candidates, setCandidates] = useState<string[]>([])
  const [url, setUrl] = useState('')

  useEffect(() => {
    if (!open || !serviceName) return
    let cancelled = false
    const dir = recipePath || ''
    client.get(`/projects/${serviceName}/tree`, { params: { dir } })
      .then(({ data }) => {
        if (cancelled) return
        const children = (data.children || []) as { name: string; path?: string; type?: string }[]
        const prefix = dir ? `${dir}/` : ''
        setCandidates(
          children
            .filter(c => c.type !== 'dir' && !String(c.name || '').startsWith('.'))
            .map(c => String(c.path || `${prefix}${c.name}`))
            .filter(Boolean),
        )
      })
      .catch(() => { if (!cancelled) setCandidates([]) })
    return () => { cancelled = true }
  }, [open, serviceName, recipePath])

  const addUrl = () => {
    const value = url.trim()
    if (!value) return
    if (!/^https?:\/\//i.test(value)) {
      message.error('A context link must start with http:// or https://')
      return
    }
    if (!links.includes(value)) onChange({ files, links: [...links, value] })
    setUrl('')
  }

  return (
    <Modal
      title="Additional context"
      open={open}
      onCancel={onClose}
      onOk={onClose}
      okText="Done"
      width={680}
      destroyOnClose
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <div>
          <Text strong>Project files</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {' '}— generation context only; never sent to the deployment
          </Text>
          {files.map(name => (
            <SelectedFileRow
              key={name}
              name={name}
              onOpen={onOpenFile}
              onRemove={() => onChange({ files: files.filter(x => x !== name), links })}
            />
          ))}
          <Select
            mode="multiple"
            allowClear
            style={{ width: '100%', marginTop: 4 }}
            value={[]}
            placeholder={files.length ? 'Add another project file…' : 'None'}
            onChange={(picked) => {
              const next = (picked as string[]).filter(Boolean)
              if (next.length === 0) return
              onChange({ files: [...new Set([...files, ...next])], links })
            }}
            options={candidates
              .filter(c => !files.includes(c))
              .map(c => ({ value: c, label: c }))}
          />
        </div>
        <div>
          <Text strong>Links</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {' '}— the URL and its fetched text are generation context only
          </Text>
          {links.map(value => (
            <div key={value} style={{ marginTop: 6 }}>
              <Tag closable onClose={() => onChange({ files, links: links.filter(x => x !== value) })}>
                {value}
              </Tag>
            </div>
          ))}
          <Space.Compact style={{ width: '100%', marginTop: 4 }}>
            <Input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onPressEnter={addUrl}
              placeholder="https://…"
            />
            <Button onClick={addUrl}>Add link</Button>
          </Space.Compact>
        </div>
      </Space>
    </Modal>
  )
}
