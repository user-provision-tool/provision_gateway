import { useState, useEffect } from 'react'
import { Modal, Form, Input, Select, Button, Switch, Space, Divider, message, Checkbox, Alert } from 'antd'
import { PlusOutlined, MinusCircleOutlined, GlobalOutlined } from '@ant-design/icons'
import client from '../../api/client'

interface DeployFormProps {
  open: boolean
  onClose: () => void
  onDeployed: (taskId: string) => void
  preselectedService?: string
}

export default function DeployForm({ open, onClose, onDeployed, preselectedService }: DeployFormProps) {
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(false)
  const [proxyEnabled, setProxyEnabled] = useState(false)
  const [httpsEnabled, setHttpsEnabled] = useState(false)
  const [sources, setSources] = useState<{ name: string; files: string[]; has_compose_template?: boolean }[]>([])
  const [deployableUsers, setDeployableUsers] = useState<{username:string,label:string}[]>([])

  useEffect(() => {
    if (open) {
      loadSources()
      loadProxyStatus()
      loadDeployableUsers()
    }
  }, [open])

  const loadDeployableUsers = async () => {
    try {
      const { data } = await client.get('/auth/users/deployable')
      setDeployableUsers((data.users||[]).map((u:any)=>({username:u.username,label:u.username})))
    } catch { /* ignore */ }
  }

  const loadSources = async () => {
    try {
      const { data } = await client.get('/services')
      setSources(data.services || [])
    } catch { /* ignore */ }
  }

  const loadProxyStatus = async () => {
    try {
      const { data } = await client.get('/system/proxy')
      setProxyEnabled(data.has_active === true)
    } catch { /* proxy not configured */ }
  }

  const handleDeploy = async (values: any) => {
    setLoading(true)
    try {
      // Build deploy payload
      const selectedService = sources.find(s => s.name === values.service_name)
      const payload: any = {
        user_name: values.user_name,
        service_name: values.service_name,
        project_root: values.service_name,
        label: values.label || '0',
        domain: values.domain || 'localhost',
        passwd: values.passwd || '',
        https: values.https || false,
        use_global_proxy: values.use_global_proxy || false,
      }

      // Add compose/nginx paths
      if (selectedService) {
        const composeJ2 = selectedService.files.find((f: string) => f.endsWith('.yml.j2'))
        const nginxJ2 = selectedService.files.find((f: string) => f.endsWith('.conf.j2'))
        if (composeJ2) payload.compose_template_path = composeJ2
        if (nginxJ2) payload.nginx_conf_template_path = nginxJ2
      }

      // HTTPS certs
      if (values.https) {
        payload.fullchain = values.fullchain || ''
        payload.privkey = values.privkey || ''
      }

      // Volume mapping
      if (values.volumes && values.volumes.length > 0) {
        payload.volumes = {}
        for (const v of values.volumes) {
          if (v.key && v.value) payload.volumes[v.key] = v.value
        }
      }

      // Build args
      if (values.build_args && values.build_args.length > 0) {
        payload.build_args = {}
        for (const a of values.build_args) {
          if (a.key && a.value) payload.build_args[a.key] = a.value
        }
      }

      const { data } = await client.post('/users/deploy', payload)
      message.success(`Deploy queued! Task: ${data.task_id}`)
      onDeployed(data.task_id)
      form.resetFields()
      onClose()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Deploy failed')
    } finally {
      setLoading(false)
    }
  }

  const selectedServiceName = Form.useWatch('service_name', form)

  return (
    <Modal
      title={`Deploy${selectedServiceName ? `: ${selectedServiceName}` : ''}`}
      open={open}
      onCancel={onClose}
      footer={null}
      width={640}
      destroyOnClose
    >
      <Form form={form} layout="vertical" onFinish={handleDeploy}
        initialValues={{ label: '0', domain: 'localhost', https: false }}>
        
        <Space style={{ width: '100%' }} direction="vertical" size="middle">
          {/* ---- User & Service ---- */}
          <Space.Compact block>
            <Form.Item name="user_name" label="User Name" rules={[{ required: true, message: 'Required' }]} style={{ flex: 1 }}>
              <Select showSearch placeholder="Select registered user" filterOption={(input, option) => (option?.label as string||'').toLowerCase().includes(input.toLowerCase())} options={deployableUsers.map(u=>({value:u.username,label:u.username}))} />
            </Form.Item>
            <Form.Item name="service_name" label="Service" rules={[{ required: true, message: 'Required' }]} style={{ flex: 1 }}>
              <Select
                showSearch
                placeholder="Select source project"
                options={sources.filter(s => s.has_compose_template).map(s => ({ value: s.name, label: s.name }))}
              />
            </Form.Item>
          </Space.Compact>

          <Space.Compact block>
            <Form.Item name="label" label="Label" style={{ flex: 1 }}>
              <Select options={[
                { value: '0', label: '0 (default)' },
                { value: '1', label: '1' },
                { value: '2', label: '2' },
              ]} />
            </Form.Item>
            <Form.Item name="domain" label="Domain" style={{ flex: 2 }}>
              <Input placeholder="example.com" />
            </Form.Item>
            <Form.Item name="passwd" label="Password" style={{ flex: 1 }}>
              <Input.Password placeholder="secret" />
            </Form.Item>
          </Space.Compact>

          {/* ---- HTTPS ---- */}
          <Form.Item name="https" label="Enable HTTPS" valuePropName="checked">
            <Switch onChange={(v) => setHttpsEnabled(v)} />
          </Form.Item>
          {httpsEnabled && (
            <Space.Compact block>
              <Form.Item name="fullchain" label="Fullchain Path" style={{ flex: 1 }}>
                <Input placeholder="/etc/letsencrypt/live/example.com/fullchain.pem" />
              </Form.Item>
              <Form.Item name="privkey" label="Privkey Path" style={{ flex: 1 }}>
                <Input placeholder="/etc/letsencrypt/live/example.com/privkey.pem" />
              </Form.Item>
            </Space.Compact>
          )}

          <Divider plain>Volume Mapping (optional)</Divider>
          <Form.List name="volumes">
            {(fields, { add, remove }) => (
              <>
                {fields.map(({ key, name, ...rest }) => (
                  <Space key={key} style={{ display: 'flex', marginBottom: 8 }} align="baseline">
                    <Form.Item {...rest} name={[name, 'key']} rules={[{ required: true, message: 'Volume name' }]}>
                      <Input placeholder="app_data" style={{ width: 180 }} />
                    </Form.Item>
                    <span>→</span>
                    <Form.Item {...rest} name={[name, 'value']} rules={[{ required: true, message: 'Host path' }]}>
                      <Input placeholder="/srv/provision/user-data/alice/app" style={{ width: 320 }} />
                    </Form.Item>
                    <Button type="text" danger icon={<MinusCircleOutlined />} onClick={() => remove(name)} />
                  </Space>
                ))}
                <Button type="dashed" onClick={() => add()} block icon={<PlusOutlined />}>Add Volume</Button>
              </>
            )}
          </Form.List>

          <Divider plain>Build Args (optional)</Divider>
          <Form.List name="build_args">
            {(fields, { add, remove }) => (
              <>
                {fields.map(({ key, name, ...rest }) => (
                  <Space key={key} style={{ display: 'flex', marginBottom: 8 }} align="baseline">
                    <Form.Item {...rest} name={[name, 'key']} rules={[{ required: true, message: 'Arg name' }]}>
                      <Input placeholder="HTTP_PROXY" style={{ width: 180 }} />
                    </Form.Item>
                    <span>→</span>
                    <Form.Item {...rest} name={[name, 'value']} rules={[{ required: true, message: 'Arg value' }]}>
                      <Input placeholder="http://proxy:8080" style={{ width: 320 }} />
                    </Form.Item>
                    <Button type="text" danger icon={<MinusCircleOutlined />} onClick={() => remove(name)} />
                  </Space>
                ))}
                <Button type="dashed" onClick={() => add()} block icon={<PlusOutlined />}>Add Build Arg</Button>
              </>
            )}
          </Form.List>

          {/* ---- Global Proxy ---- */}
          <Form.Item name="use_global_proxy" valuePropName="checked">
            <Checkbox disabled={!proxyEnabled}>
              <GlobalOutlined /> Use global proxy for this deployment
              {!proxyEnabled && <span style={{ color: '#999', fontSize: 12 }}> (enable in Settings first)</span>}
              {proxyEnabled && <span style={{ color: '#52c41a', fontSize: 12 }}> (proxy is enabled)</span>}
            </Checkbox>
          </Form.Item>

          <Space style={{ justifyContent: 'flex-end', width: '100%' }}>
            <Button onClick={onClose}>Cancel</Button>
            <Button type="primary" htmlType="submit" loading={loading} icon={<span>🚀</span>}>
              Deploy
            </Button>
          </Space>
        </Space>
      </Form>
    </Modal>
  )
}
