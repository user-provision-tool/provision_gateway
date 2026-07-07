import { useState, useEffect } from 'react'
import { Typography, Card, Table, Button, Modal, Form, Input, Space, message, Tag, Popconfirm, Tooltip } from 'antd'
import { PlusOutlined, DeleteOutlined, ReloadOutlined, SafetyCertificateOutlined, FolderOpenOutlined } from '@ant-design/icons'
import { usePolling } from '../hooks/usePolling'
import client from '../api/client'

const { Title, Text, Paragraph } = Typography

interface SSLDomain {
  domain: string
  fullchain_path: string
  privkey_path: string
  created_at: string
  expiry_date: string
  days_left: number
}

const EXPIRY_POLL_MS = 60_000 // check cert expiry every 60 seconds

export default function SSLPage() {
  const [domains, setDomains] = useState<SSLDomain[]>([])
  const [loading, setLoading] = useState(true)
  const [modalOpen, setModalOpen] = useState(false)
  const [uploadLoading, setUploadLoading] = useState(false)
  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({})
  const [form] = Form.useForm()

  const fetchCerts = async () => {
    setLoading(true)
    try {
      const { data } = await client.get('/system/ssl-certs')
      setDomains(data.domains || [])
    } catch { /* silent */ }
    finally { setLoading(false) }
  }

  useEffect(() => { fetchCerts() }, [])
  usePolling(fetchCerts, 30000)

  const handleUpload = async (values: any) => {
    setUploadLoading(true)
    try {
      const fd = new FormData()
      fd.append('domain', values.domain)
      fd.append('fullchain', values.fullchain || '')
      fd.append('privkey', values.privkey || '')
      fd.append('ssl_path', values.ssl_path || '')
      await client.post('/system/ssl-certs', fd, {
        headers: { 'Content-Type': 'multipart/form-data' }
      })
      message.success(`SSL cert for ${values.domain} saved`)
      setModalOpen(false)
      form.resetFields()
      fetchCerts()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to upload')
    } finally { setUploadLoading(false) }
  }

  const handleDelete = async (domain: string) => {
    try {
      await client.delete(`/system/ssl-certs/${domain}`)
      message.success(`SSL cert for ${domain} deleted`)
      fetchCerts()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to delete')
    }
  }

  const handleRefresh = async (domain: string) => {
    setRefreshing(prev => ({ ...prev, [domain]: true }))
    try {
      const { data } = await client.post(`/system/ssl-certs/${domain}/refresh`)
      message.success(`SSL cert for ${domain} refreshed (expires ${data.expiry_date})`)
      fetchCerts()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to refresh')
    } finally {
      setRefreshing(prev => ({ ...prev, [domain]: false }))
    }
  }

  const columns = [
    { title: 'Domain', dataIndex: 'domain', key: 'domain',
      render: (d: string) => <Space><SafetyCertificateOutlined style={{color:'#1677ff'}}/><Text strong>{d}</Text></Space> },
    { title: 'Certificate', dataIndex: 'fullchain_path', key: 'cert',
      render: (p: string) => <Tag color="green">fullchain.pem</Tag> },
    { title: 'Private Key', dataIndex: 'privkey_path', key: 'key',
      render: (p: string) => <Tag color="orange">privkey.pem</Tag> },
    { title: 'Expires', dataIndex: 'days_left', key: 'expiry',
      render: (days: number, r: SSLDomain) => {
        if (days < 0) return <Tag>unknown</Tag>
        if (days <= 7) return <Tooltip title={`Expires ${r.expiry_date}`}><Tag color="red">{days}d</Tag></Tooltip>
        if (days <= 30) return <Tooltip title={`Expires ${r.expiry_date}`}><Tag color="orange">{days}d</Tag></Tooltip>
        return <Tooltip title={`Expires ${r.expiry_date}`}><Tag color="green">{days}d</Tag></Tooltip>
      }},
    { title: 'Actions', key: 'actions',
      render: (_: any, r: SSLDomain) => (
        <Space>
          <Tooltip title="Re-import from source path">
            <Button size="small" icon={<ReloadOutlined/>} loading={refreshing[r.domain]}
              onClick={() => handleRefresh(r.domain)} />
          </Tooltip>
          <Popconfirm title={`Delete cert for ${r.domain}?`} onConfirm={() => handleDelete(r.domain)}>
            <Button size="small" danger icon={<DeleteOutlined/>} />
          </Popconfirm>
        </Space>
      )},
  ]

  return (
    <div>
      <div style={{display:'flex',justifyContent:'space-between',marginBottom:16,flexWrap:'wrap',gap:8}}>
        <Title level={3} style={{margin:0}}>SSL Certificates</Title>
        <Space>
          <Button icon={<ReloadOutlined/>} onClick={fetchCerts}>Refresh</Button>
          <Button type="primary" icon={<PlusOutlined/>} onClick={() => setModalOpen(true)}>Add Certificate</Button>
        </Space>
      </div>

      <Card>
        <Paragraph type="secondary" style={{marginBottom:16}}>
          Import SSL certificates by providing the path to the Let's Encrypt live directory
          (e.g. <Text code>/etc/letsencrypt/live/example.com</Text>).
          The <Text code>fullchain.pem</Text> and <Text code>privkey.pem</Text> will be read
          from that directory. Once imported, select the domain when deploying with HTTPS enabled.
        </Paragraph>
        <Table dataSource={domains} columns={columns} rowKey="domain" pagination={false}
          loading={loading} locale={{ emptyText: 'No SSL certificates uploaded yet' }}/>
      </Card>

      <Modal title="Add SSL Certificate" open={modalOpen} onCancel={() => { setModalOpen(false); form.resetFields() }}
        footer={null} width={600}>
        <Form form={form} layout="vertical" onFinish={handleUpload}
          initialValues={{ fullchain: '', privkey: '', ssl_path: '' }}>
          <Form.Item name="domain" label="Domain Name" rules={[{required:true,message:'Enter domain name'}]}>
            <Input placeholder="example.com"/>
          </Form.Item>
          <Form.Item name="ssl_path" label="SSL Directory Path"
            tooltip="Path to the directory containing fullchain.pem and privkey.pem (e.g. /etc/letsencrypt/live/example.com)">
            <Input prefix={<FolderOpenOutlined/>} placeholder="/etc/letsencrypt/live/example.com"/>
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={uploadLoading} block icon={<PlusOutlined/>}>
            Import Certificate
          </Button>
        </Form>
      </Modal>
    </div>
  )
}
