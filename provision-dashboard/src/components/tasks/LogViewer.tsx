import { useEffect, useRef, useState } from 'react'
import { Button, Space, Typography } from 'antd'
import { CopyOutlined, DownloadOutlined } from '@ant-design/icons'

const { Text } = Typography

interface LogViewerProps {
  taskId: string
  tail?: number
  token: string
}

export default function LogViewer({ taskId, tail = 200, token }: LogViewerProps) {
  const [lines, setLines] = useState<string[]>([])
  const containerRef = useRef<HTMLDivElement>(null)
  const [autoScroll, setAutoScroll] = useState(true)

  useEffect(() => {
    setLines([])
    const url = `/api/tasks/${taskId}/log?tail=${tail}&follow=true&token=${encodeURIComponent(token)}`
    const es = new EventSource(url)
    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data)
        if (data.line !== undefined) {
          setLines(prev => [...prev, data.line])
        }
      } catch {
        setLines(prev => [...prev, e.data])
      }
    }
    es.onerror = () => es.close()
    return () => es.close()
  }, [taskId, tail, token])

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [lines, autoScroll])

  const handleCopy = () => navigator.clipboard.writeText(lines.join('\n'))
  const handleDownload = () => {
    const blob = new Blob([lines.join('\n')], { type: 'text/plain' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `task-${taskId}.log`
    a.click()
  }

  return (
    <div>
      <Space style={{ marginBottom: 8 }}>
        <Button size="small" icon={<CopyOutlined />} onClick={handleCopy}>Copy</Button>
        <Button size="small" icon={<DownloadOutlined />} onClick={handleDownload}>Download</Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {autoScroll ? '⬇ auto-scroll' : '⬆ paused'}
        </Text>
      </Space>
      <div
        ref={containerRef}
        onScroll={() => {
          if (containerRef.current) {
            const { scrollTop, scrollHeight, clientHeight } = containerRef.current
            setAutoScroll(scrollHeight - scrollTop - clientHeight < 50)
          }
        }}
        style={{
          background: '#1e1e1e', color: '#d4d4d4', fontFamily: 'monospace', fontSize: 12,
          padding: 12, borderRadius: 4, maxHeight: '60vh', overflow: 'auto', whiteSpace: 'pre-wrap',
        }}
      >
        {lines.map((line, i) => <div key={i}>{line}</div>)}
      </div>
    </div>
  )
}
