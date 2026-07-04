import { useState, useEffect } from 'react'
import { Typography, Card, Form, Input, Select, Button, message, Space, Spin, Alert } from 'antd'
import { SaveOutlined, ApiOutlined, RobotOutlined, KeyOutlined } from '@ant-design/icons'
import client from '../api/client'

const { Title, Text } = Typography

export default function SettingsPage() {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<any>(null)
  const [form] = Form.useForm()

  useEffect(() => { loadConfig() }, [])

  const loadConfig = async () => {
    try {
      const { data } = await client.get('/llm/config')
      form.setFieldsValue({ mode: data.mode||'local_agent', agent_url: data.agent_url||'', agent_model: data.agent_model||'', byok_base_url: data.byok_base_url||'', byok_model: data.byok_model||'', system_prompt: data.system_prompt||'' })
    } catch { message.error('Failed to load settings') }
    finally { setLoading(false) }
  }

  const handleSave = async (values: any) => {
    setSaving(true)
    try { await client.put('/llm/config', values); message.success('Saved') }
    catch (err: any) { message.error(err.response?.data?.detail||'Failed') }
    finally { setSaving(false) }
  }

  const handleTest = async () => {
    setTesting(true); setTestResult(null)
    try {
      const { data } = await client.post('/llm/test')
      setTestResult(data)
      message[data.success?'success':'error'](data.success?`Connected! Model: ${data.model}`:data.error||'Failed')
    } catch (err: any) { setTestResult({success:false,error:err.response?.data?.detail||'Test failed'}) }
    finally { setTesting(false) }
  }

  const mode = Form.useWatch('mode', form)
  if (loading) return <Spin/>

  return (
    <div>
      <Title level={3}>Settings</Title>
      <Card title={<span><RobotOutlined/> LLM Configuration</span>} style={{marginBottom:16}}>
        <Form form={form} layout="vertical" onFinish={handleSave} initialValues={{mode:'local_agent'}}>
          <Form.Item name="mode" label="LLM Mode">
            <Select options={[{value:'local_agent',label:'Local Agent (Ollama-compatible)'},{value:'byok',label:'Bring Your Own Key (OpenAI-compatible)'}]}/>
          </Form.Item>
          {mode==='local_agent'&&<>
            <Form.Item name="agent_url" label="Agent URL"><Input placeholder="http://localhost:11434/v1" prefix={<ApiOutlined/>}/></Form.Item>
            <Form.Item name="agent_model" label="Model Name"><Input placeholder="llama3.1:8b"/></Form.Item>
          </>}
          {mode==='byok'&&<>
            <Form.Item name="byok_base_url" label="API Base URL"><Input placeholder="https://api.deepseek.com/v1" prefix={<ApiOutlined/>}/></Form.Item>
            <Form.Item name="byok_model" label="Model Name"><Input placeholder="deepseek-chat"/></Form.Item>
            <Form.Item name="byok_api_key" label="API Key"><Input.Password placeholder="sk-..." prefix={<KeyOutlined/>}/></Form.Item>
          </>}
          <Form.Item name="system_prompt" label="System Prompt (optional)"><Input.TextArea rows={4} placeholder="You are a DevOps assistant..."/></Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={saving} icon={<SaveOutlined/>}>Save</Button>
            <Button onClick={handleTest} loading={testing} icon={<RobotOutlined/>}>Test Connection</Button>
          </Space>
        </Form>
        {testResult&&<Alert type={testResult.success?'success':'error'} message={testResult.success?'Connected':'Failed'} description={testResult.success?`Model: ${testResult.model}`:testResult.error||'Unknown error'} style={{marginTop:16}} closable/>}
      </Card>
      <Card title="System Info"><Space direction="vertical"><Text><strong>Gateway:</strong> v1.0.0</Text><Text><strong>Provision API:</strong> provision-api:8000</Text></Space></Card>
    </div>
  )
}
