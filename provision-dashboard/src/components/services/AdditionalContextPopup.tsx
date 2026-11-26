import { useState } from 'react'
import { Button, Input, Modal, Space, Tag, Typography, message } from 'antd'
import ProjectBrowser from '../projects/ProjectBrowser'
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
  onOpenFile?: (name: string) => void
}

/** C16 — the Additional context popup IS the project tree browser: the same
 *  tree + preview/editor used when a project is opened on Source Projects,
 *  with the `add to context` button left of `Edit`, plus the URL input.
 *  Both kinds are generation context only: never sent to the deployment. */
export default function AdditionalContextPopup({
  open, onClose, serviceName, files, links, onChange, onOpenFile,
}: Props) {
  const [url, setUrl] = useState('')

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
      width={960}
      destroyOnClose
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <div>
          <Text strong>Project files</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {' '}— click a file to preview it, then “add to context”; generation context only
          </Text>
          {files.map(name => (
            <SelectedFileRow
              key={name}
              name={name}
              onOpen={onOpenFile}
              onRemove={() => onChange({ files: files.filter(x => x !== name), links })}
            />
          ))}
          {serviceName ? (
            <div style={{ marginTop: 8 }}>
              <ProjectBrowser
                serviceName={serviceName}
                height="52vh"
                contextFiles={files}
                onAddToContext={(path) => {
                  if (!files.includes(path)) onChange({ files: [...files, path], links })
                }}
              />
            </div>
          ) : null}
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
