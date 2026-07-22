import { useState, useEffect, useRef } from 'react'
import { Typography, Card, Form, Input, Select, Button, message, Space, Spin, Alert, Empty, Divider, Tag, Switch, Popconfirm } from 'antd'
import { SaveOutlined, ApiOutlined, RobotOutlined, KeyOutlined, GlobalOutlined, CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined, DeleteOutlined } from '@ant-design/icons'
import { useAuth } from '../hooks/useAuth'
import client from '../api/client'

const { Title, Text } = Typography

export default function SettingsPage() {
  const { admin } = useAuth()
  const isAdmin = admin?.role === 'admin'

  return (
    <div>
      <Title level={3}>Settings</Title>
      {isAdmin ? <><LlmPanel /><ProxyPanel /></> : <Card><Empty description="Settings management requires admin role."/></Card>}
      <Card title="System Info"><Space direction="vertical"><Text><strong>Gateway:</strong> v1.0.0</Text><Text><strong>Provision API:</strong> provision-api:8000</Text></Space></Card>
    </div>
  )
}

// ---- LLM Panel (multi-config cards) ----
function LlmPanel() {
  const [configs, setConfigs] = useState<any[]>([])
  const [active, setActive] = useState<any>(null)
  const [loaded, setLoaded] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<any>(null)
  const [form] = Form.useForm()

  useEffect(() => { loadConfigs() }, [])

  const loadConfigs = async () => {
    try {
      const { data } = await client.get('/llm/configs')
      setConfigs(data.configs || [])
      setActive(data.active)
    } catch { /* ignore */ }
    finally { setLoaded(true) }
  }

  const handleAdd = async (values: any) => {
    setSaving(true)
    try {
      await client.post('/llm/configs', values)
      message.success('LLM config added')
      form.resetFields()
      loadConfigs()
    } catch (err: any) { message.error(err.response?.data?.detail || 'Failed') }
    finally { setSaving(false) }
  }

  const handleActivate = async (id: number) => {
    try {
      await client.put(`/llm/configs/${id}/activate`)
      message.success('Activated')
      loadConfigs()
    } catch (err: any) { message.error('Failed') }
  }

  const handleDelete = async (id: number) => {
    try {
      await client.delete(`/llm/configs/${id}`)
      message.success('Deleted')
      loadConfigs()
    } catch (err: any) { message.error('Failed') }
  }

  const handleTest = async () => {
    setTesting(true); setTestResult(null)
    try {
      const { data } = await client.post('/llm/test')
      setTestResult(data)
      message[data.success ? 'success' : 'error'](data.success ? `Connected: ${data.model}` : data.error || 'Failed')
    } catch (err: any) { setTestResult({ success: false, error: err.response?.data?.detail || 'Test failed' }) }
    finally { setTesting(false) }
  }

  return (
    <Card title={<span><RobotOutlined /> LLM Configuration</span>} style={{ marginBottom: 16 }}>
      {/* Config cards */}
      {configs.map((cfg: any) => (
        <div key={cfg.id} style={{
          border: `1px solid ${cfg.is_active ? '#1677ff' : '#d9d9d9'}`,
          borderRadius: 8, padding: '12px 16px', marginBottom: 8,
          background: cfg.is_active ? '#e6f4ff' : '#fafafa',
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
            <Space>
              <Text strong={cfg.is_active} style={{ fontSize: 14 }}>
                {cfg.mode === 'byok' ? '🔑 BYOK' : '🖥️ Local'} — {cfg.byok_model || cfg.agent_model || 'unnamed'}
              </Text>
              {cfg.byok_base_url && <Text type="secondary" style={{ fontSize: 12 }}>{cfg.byok_base_url}</Text>}
              {cfg.is_active && <Tag color="blue">ACTIVE</Tag>}
            </Space>
            <Space>
              {!cfg.is_active && (
                <Button size="small" type="primary" onClick={() => handleActivate(cfg.id)}>Activate</Button>
              )}
              <Popconfirm title="Delete?" onConfirm={() => handleDelete(cfg.id)}>
                <Button size="small" danger icon={<DeleteOutlined />} />
              </Popconfirm>
            </Space>
          </div>
        </div>
      ))}
      {configs.length === 0 && loaded && <Empty description="No LLM configs yet" style={{ marginBottom: 16 }} />}

      <Divider plain>Add LLM Configuration</Divider>
      <Form form={form} layout="vertical" onFinish={handleAdd} initialValues={{ mode: 'byok' }}>
        <Form.Item name="mode" label="Mode">
          <Select options={[{ value: 'byok', label: 'Bring Your Own Key (OpenAI-compatible)' }]}
            disabled
          />
          <Text type="secondary" style={{fontSize:11,display:'block',marginTop:4}}>
            BYOK is the currently supported mode. Local Agent and Provision Agent are planned for future releases.
          </Text>
        </Form.Item>
        <Form.Item name="byok_base_url" label="API Base URL"><Input placeholder="https://api.deepseek.com/v1" prefix={<ApiOutlined />} /></Form.Item>
        <Form.Item name="byok_model" label="Model Name"><Input placeholder="deepseek-chat" /></Form.Item>
        <Form.Item name="byok_api_key" label="API Key"><Input.Password placeholder="sk-..." prefix={<KeyOutlined />} /></Form.Item>
        <Form.Item name="agent_url" label="Agent URL (for local mode)"><Input placeholder="http://localhost:11434/v1" /></Form.Item>
        <Form.Item name="agent_model" label="Agent Model"><Input placeholder="llama3.1:8b" /></Form.Item>
        <Form.Item name="system_prompt" label="System Prompt"><Input.TextArea rows={3} placeholder="You are a DevOps assistant..." /></Form.Item>
        <Space>
          <Button type="primary" htmlType="submit" loading={saving} icon={<SaveOutlined />}>Add Config</Button>
          <Button onClick={handleTest} loading={testing} icon={<RobotOutlined />}>Test Active</Button>
        </Space>
      </Form>
      {testResult && <Alert type={testResult.success ? 'success' : 'error'} message={testResult.success ? 'Connected' : 'Failed'} description={testResult.success ? `Model: ${testResult.model}` : testResult.error} style={{ marginTop: 16 }} closable />}
    </Card>
  )
}

// ---- Proxy Panel (multi-config cards) ----
function ProxyPanel() {
  const [proxyForm] = Form.useForm()
  const [proxySaving, setProxySaving] = useState(false)
  const [configs, setConfigs] = useState<any[]>([])
  const [proxyLoaded, setProxyLoaded] = useState(false)
  const pollingRef = useRef<any>(null)

  useEffect(() => { loadProxy(); return () => { if (pollingRef.current) clearInterval(pollingRef.current) } }, [])

  const loadProxy = async () => {
    try {
      const { data } = await client.get('/system/proxy')
      setConfigs(data.configs || [])
      startAutoPolling()
    } catch { /* ignore */ }
    finally { setProxyLoaded(true) }
  }

  const startAutoPolling = () => {
    if (pollingRef.current) clearInterval(pollingRef.current)
    pollingRef.current = setInterval(async () => {
      try { await client.post('/system/proxy/test'); const r = await client.get('/system/proxy'); setConfigs(r.data.configs || []) } catch { /* ignore */ }
    }, 10000)
  }

  const handleAddConfig = async (values: any) => {
    setProxySaving(true)
    try { await client.post('/system/proxy', values); message.success('Config added'); proxyForm.resetFields(['host', 'port', 'name']); loadProxy() }
    catch (err: any) { message.error(err.response?.data?.detail || 'Failed') }
    finally { setProxySaving(false) }
  }

  const handleActivate = async (id: number) => {
    try { await client.put(`/system/proxy/${id}/activate`); message.success('Activated'); loadProxy() }
    catch (err: any) { message.error(err.response?.data?.detail || 'Cannot activate') }
  }

  const handleDeactivate = async () => {
    try { await client.post('/system/proxy/deactivate'); message.success('Deactivated'); loadProxy() }
    catch (err: any) { message.error('Failed') }
  }

  const handleDelete = async (id: number) => {
    try { await client.delete(`/system/proxy/${id}`); message.success('Deleted'); loadProxy() }
    catch (err: any) { message.error('Failed') }
  }

  return (
    <Card title={<span><GlobalOutlined /> Global Proxy</span>} style={{ marginBottom: 16 }}>
      {configs.map((cfg: any) => {
        const isActive = cfg.is_active; const isReachable = cfg.reachable === true
        return (
          <div key={cfg.id} style={{ border: `1px solid ${isActive ? '#1677ff' : '#d9d9d9'}`, borderRadius: 8, padding: '12px 16px', marginBottom: 8, background: isActive ? '#e6f4ff' : '#fafafa' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
              <Space>
                <Text strong={isActive} style={{ fontSize: 14 }}>{cfg.name || cfg.url}</Text>
                {isReachable ? <Tag icon={<CheckCircleOutlined />} color="success">reachable</Tag> : cfg.reachable === false ? <Tag icon={<CloseCircleOutlined />} color="error">unreachable</Tag> : <Tag icon={<LoadingOutlined spin />} color="processing">checking</Tag>}
              </Space>
              <Space>
                <Switch checked={isActive} disabled={!isReachable} onChange={(checked) => checked ? handleActivate(cfg.id) : handleDeactivate()} checkedChildren="ON" unCheckedChildren="OFF" />
                <Popconfirm title="Delete?" onConfirm={() => handleDelete(cfg.id)}><Button size="small" danger icon={<DeleteOutlined />} /></Popconfirm>
              </Space>
            </div>
            {!isReachable && <div style={{ marginTop: 4 }}><Text type="secondary" style={{ fontSize: 11 }}>Cannot enable — proxy is unreachable</Text></div>}
          </div>
        )
      })}
      {configs.length === 0 && proxyLoaded && <Empty description="No proxy configs saved yet" style={{ marginBottom: 16 }} />}
      <Divider plain>Add Configuration</Divider>
      <Form form={proxyForm} layout="vertical" onFinish={handleAddConfig} initialValues={{ protocol: 'http', port: 7897 }}>
        <Space.Compact block>
          <Form.Item name="name" label="Name" style={{ flex: 1 }}><Input placeholder="e.g. Host Proxy" /></Form.Item>
          <Form.Item name="protocol" label="Protocol" style={{ flex: 1 }}><Select options={[{ value: 'http', label: 'HTTP' }, { value: 'https', label: 'HTTPS' }, { value: 'socks5', label: 'SOCKS5' }]} /></Form.Item>
        </Space.Compact>
        <Space.Compact block>
          <Form.Item name="host" label="Host" style={{ flex: 2 }} rules={[{ required: true }]}><Input placeholder="172.18.0.1" /></Form.Item>
          <Form.Item name="port" label="Port" style={{ flex: 1 }} rules={[{ required: true }]}><Input style={{ width: '100%' }} /></Form.Item>
        </Space.Compact>
        <Space.Compact block>
          <Form.Item name="username" label="Username" style={{ flex: 1 }}><Input placeholder="Optional" /></Form.Item>
          <Form.Item name="password" label="Password" style={{ flex: 1 }}><Input.Password placeholder="Optional" /></Form.Item>
        </Space.Compact>
        <Button type="primary" htmlType="submit" loading={proxySaving} icon={<SaveOutlined />}>Add Configuration</Button>
      </Form>
    </Card>
  )
}
