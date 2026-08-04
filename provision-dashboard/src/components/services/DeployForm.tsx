import { useState, useEffect } from 'react'
import { Modal, Form, Input, Select, Button, Switch, Space, Divider, message, Checkbox, Alert, Spin, Tag, Typography } from 'antd'
import { PlusOutlined, MinusCircleOutlined, GlobalOutlined, RobotOutlined } from '@ant-design/icons'
import Editor from '@monaco-editor/react'
import client from '../../api/client'

const { Text } = Typography

// Language detection for Monaco based on filename
function getLanguage(filename: string): string {
  if (filename.endsWith('.yml') || filename.endsWith('.yaml')) return 'yaml'
  if (filename.endsWith('.conf') || filename.includes('nginx')) return 'nginx'
  if (filename.includes('Dockerfile')) return 'dockerfile'
  if (filename.endsWith('.env') || filename.endsWith('.sh')) return 'shell'
  return 'plaintext'
}

interface DeployFormProps {
  open: boolean
  onClose: () => void
  onDeployed: (taskId: string, user: string, service: string, label: string) => void
  preselectedService?: string
}

export default function DeployForm({ open, onClose, onDeployed, preselectedService }: DeployFormProps) {
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(false)
  const [proxyEnabled, setProxyEnabled] = useState(false)
  const [httpsEnabled, setHttpsEnabled] = useState(false)
  const [sources, setSources] = useState<{ name: string; files: string[]; has_compose_template?: boolean }[]>([])
  const [deployableUsers, setDeployableUsers] = useState<{username:string,label:string}[]>([])
  const [sslDomains, setSslDomains] = useState<{domain:string, fullchain_path:string, privkey_path:string}[]>([])
  const [selectedSslDomain, setSelectedSslDomain] = useState<string>('')

  // Auto-deploy / missing files state
  const [checkingMissing, setCheckingMissing] = useState(false)
  const [missingFiles, setMissingFiles] = useState<string[]>([])
  const [autoDeploy, setAutoDeploy] = useState(true)
  const [generatingFiles, setGeneratingFiles] = useState(false)
  const [generatedFiles, setGeneratedFiles] = useState<Record<string,string>>({})
  // Editor modal for reviewing generated files (clickable → built-in editor)
  const [editorModalOpen, setEditorModalOpen] = useState(false)
  const [editorFileName, setEditorFileName] = useState('')
  const [editorContent, setEditorContent] = useState('')
  const [editorSaving, setEditorSaving] = useState(false)

  // GAP-003: Auto-computed next label state
  const [nextLabel, setNextLabel] = useState<string>('0')
  const [computingLabel, setComputingLabel] = useState(false)

  // GAP-003: Compute next label when user+service selections change
  const computeNextLabel = async (userName: string, serviceName: string) => {
    if (!userName || !serviceName) {
      setNextLabel('0')
      return
    }
    setComputingLabel(true)
    try {
      const { data } = await client.get(`/users/${userName}/${serviceName}/next-label`)
      const label = data.label || '0'
      setNextLabel(label)
      form.setFieldsValue({ label })
    } catch {
      // Fall back to 0 if provision-api is unreachable
      setNextLabel('0')
      form.setFieldsValue({ label: '0' })
    } finally {
      setComputingLabel(false)
    }
  }

  useEffect(() => {
    if (open) {
      loadSources()
      loadProxyStatus()
      loadDeployableUsers()
      loadSslDomains()
      // Reset auto-deploy state
      setMissingFiles([])
      setCheckError(null)
      setScanContext(null)
      setAutoDeploy(true)
      setGeneratedFiles({})
      setEditorModalOpen(false)
      setNextLabel('0')
      // Preselect service if provided
      if (preselectedService) {
        form.setFieldsValue({ service_name: preselectedService })
        checkMissingFiles(preselectedService)
      }
    }
  }, [open, preselectedService])

  const loadSslDomains = async () => {
    try {
      const { data } = await client.get('/system/ssl-certs')
      setSslDomains(data.domains || [])
    } catch { /* ignore */ }
  }

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

  // Cache scan context from check-missing-files for LLM generation
  const [scanContext, setScanContext] = useState<any>(null)
  const [checkError, setCheckError] = useState<string | null>(null)

  // Check for missing essential files when service selection changes
  const checkMissingFiles = async (serviceName: string) => {
    if (!serviceName) { setMissingFiles([]); setScanContext(null); return }
    setCheckingMissing(true)
    try {
      const { data } = await client.get(`/services/${serviceName}/check-missing-files`)
      setMissingFiles(data.missing || [])
      setCheckError(null)
      if (data.scan_context) {
        setScanContext(data.scan_context)
      }
    } catch (err: any) {
      setCheckError(err?.message || 'Check failed — try again')
      setMissingFiles([])
      setScanContext(null)
    } finally { setCheckingMissing(false) }
  }

  // Generate missing files via LLM — shared by the manual "Generate" button
  // and the auto-completion path in handleDeploy
  const generateFilesFor = async (fileTypes: string[], ctx: any): Promise<Record<string,string>> => {
    const results: Record<string,string> = {}
    for (const fileType of fileTypes) {
      const typeMap: Record<string, string> = {
        'docker-compose': 'docker_compose',
        'nginx.conf': 'nginx_conf',
        '.env': 'env_file',
        'Dockerfile': 'dockerfile',
      }
      const genType = typeMap[fileType] || 'docker_compose'
      try {
        const { data } = await client.post('/llm/generate', { type: genType, context: ctx })
        if (data.generated_content) {
          const filename = fileType === 'docker-compose' ? 'docker-compose.yml' :
                          fileType === 'nginx.conf' ? 'nginx.conf' : fileType
          results[filename] = data.generated_content
        }
      } catch { /* skip individual failures */ }
    }
    return results
  }

  // Generate missing files via LLM (manual button)
  const generateMissingFiles = async () => {
    setGeneratingFiles(true)
    try {
      // Use scan context from check-missing-files response (enriched with repo scan)
      const ctx = scanContext || {
        repo_description: `Service: ${form.getFieldValue('service_name') || 'unknown'}`,
        repo_files: [],
        port: 80,
        needs_db: false,
      }
      const results = await generateFilesFor(missingFiles, ctx)
      setGeneratedFiles(results)
      if (Object.keys(results).length > 0) {
        // Persist to the service project on disk immediately (not just on deploy)
        try {
          await client.post('/services/save-generated', {
            service_name: form.getFieldValue('service_name'),
            files: results,
          })
          message.success(`LLM generated & saved ${Object.keys(results).length} file(s) — review, then deploy`)
        } catch {
          message.warning(`Generated ${Object.keys(results).length} file(s), but saving to disk failed`)
        }
      } else {
        message.warning('LLM could not generate any files. Configure BYOK LLM in Settings.')
      }
    } catch { message.error('Failed to generate files via LLM') }
    finally { setGeneratingFiles(false) }
  }

  // Save an edited generated file back to state + disk
  const saveEditorFile = async () => {
    if (!editorFileName) return
    setEditorSaving(true)
    try {
      // Merge the edited content into the generated-files state
      setGeneratedFiles(prev => ({ ...prev, [editorFileName]: editorContent }))
      // Persist to the service project on disk
      await client.post('/services/save-generated', {
        service_name: form.getFieldValue('service_name'),
        files: { [editorFileName]: editorContent },
      })
      message.success(`${editorFileName} saved`)
      setEditorModalOpen(false)
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to save file')
    } finally {
      setEditorSaving(false)
    }
  }

  const handleDeploy = async (values: any) => {
    setLoading(true)
    try {
      // Snapshot previously generated files; anything generated during this call is tracked locally
      let gen = generatedFiles

      // No generated files available yet:
      //  - Auto Templates Completion ON  → generate ONLY missing essential files via LLM and deploy.
      //    If no files are missing (service is ready), skip generation entirely — do NOT
      //    overwrite existing project files.
      //  - Auto Templates Completion OFF → block deploy while essential files are missing (GAP-002)
      if (Object.keys(gen).length === 0 && missingFiles.length > 0) {
        if (autoDeploy) {
          const ctx = scanContext || {
            repo_description: `Service: ${values.service_name || 'unknown'}`,
            repo_files: [],
            port: 80,
            needs_db: false,
          }
          gen = await generateFilesFor(missingFiles, ctx)
          if (Object.keys(gen).length === 0) {
            message.error('Auto Templates Completion could not generate the missing files. Configure BYOK LLM in Settings.')
            setLoading(false)
            return
          }
          setGeneratedFiles(gen)
          message.success(`LLM generated ${Object.keys(gen).length} file(s) — deploying...`)
        } else {
          message.error('Cannot deploy: missing essential files. Use LLM auto-generation or upload files manually.')
          setLoading(false)
          return
        }
      }

      const selectedService = sources.find(s => s.name === values.service_name)

      // Save any LLM-generated files first — execute regardless of autoDeploy state
      if (Object.keys(gen).length > 0) {
        await client.post('/services/save-generated', {
          service_name: values.service_name,
          files: gen,
        })
      }

      // Build deploy payload
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

      // Add compose/nginx paths — use consistent naming per design.md (G10)
      if (selectedService) {
        const composeJ2 = selectedService.files.find((f: string) => f.endsWith('.yml.j2'))
        const nginxJ2 = selectedService.files.find((f: string) => f.endsWith('.conf.j2'))
        // Fallback: look for regular (non-.j2) compose/nginx files that already exist in the project
        const composeFile = selectedService.files.find((f: string) =>
          (f.endsWith('.yml') || f.endsWith('.yaml')) && !f.endsWith('.j2'))
        const nginxFile = selectedService.files.find((f: string) =>
          f.endsWith('.conf') && !f.endsWith('.j2'))

        if (composeJ2) payload.compose_template_path = composeJ2
        else if (gen['docker-compose.yml']) payload.compose_file_path = 'docker-compose.yml'
        else if (composeFile) payload.compose_file_path = composeFile

        if (nginxJ2) payload.nginx_conf_template_path = nginxJ2
        else if (gen['nginx.conf']) payload.nginx_conf_file_path = 'nginx.conf'
        else if (nginxFile) payload.nginx_conf_file_path = nginxFile
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
      onDeployed(data.task_id, values.user_name, values.service_name, values.label || '0')
      form.resetFields()
      onClose()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Deploy failed')
    } finally {
      setLoading(false)
    }
  }

  const selectedServiceName = Form.useWatch('service_name', form)
  const selectedUserName = Form.useWatch('user_name', form)

  return (
    <Modal
      title={`Deploy${selectedServiceName ? `: ${selectedServiceName}` : ''}`}
      open={open}
      onCancel={onClose}
      footer={null}
      width={640}
      destroyOnClose
    >
      <Form form={form} layout="vertical" onFinish={handleDeploy} preserve={false}
        initialValues={{ label: '0', domain: 'localhost', https: false }}>
        
        <Space style={{ width: '100%' }} direction="vertical" size="middle">
          {/* ---- User & Service ---- */}
          <Space.Compact block>
            <Form.Item name="user_name" label="User Name" rules={[{ required: true, message: 'Required' }]} style={{ flex: 1 }}>
              <Select showSearch placeholder="Select registered user" filterOption={(input, option) => (option?.label as string||'').toLowerCase().includes(input.toLowerCase())} options={deployableUsers.map(u=>({value:u.username,label:u.username}))}
                onChange={(val) => {
                  const svc = form.getFieldValue('service_name')
                  if (val && svc) computeNextLabel(val, svc)
                }}
              />
            </Form.Item>
            <Form.Item name="service_name" label="Service" rules={[{ required: true, message: 'Required' }]} style={{ flex: 1 }}>
              <Select
                showSearch
                placeholder="Select source project"
                options={sources.map(s => ({ value: s.name, label: s.name }))}
                onChange={(val) => {
                  checkMissingFiles(val)
                  const user = form.getFieldValue('user_name')
                  if (val && user) computeNextLabel(user, val)
                }}
              />
            </Form.Item>
          </Space.Compact>

          <Space.Compact block>
            <Form.Item name="label" label="Label" style={{ flex: 1 }}>
              <Input disabled addonAfter={computingLabel ? <Spin size="small" /> : null} />
            </Form.Item>
            <Form.Item name="domain" label="Domain" style={{ flex: 2 }}>
              <Input placeholder="example.com" disabled={!!selectedSslDomain} />
            </Form.Item>
            <Form.Item name="passwd" label="Password" style={{ flex: 1 }}>
              <Input.Password placeholder="secret" />
            </Form.Item>
          </Space.Compact>

          {/* ---- HTTPS ---- */}
          <Form.Item name="https" label="Enable HTTPS" valuePropName="checked">
            <Switch onChange={(v) => {
              setHttpsEnabled(v)
              if (!v) {
                setSelectedSslDomain('')
                form.setFieldsValue({ ssl_domain: undefined, fullchain: '', privkey: '' })
              }
            }} />
          </Form.Item>
          {httpsEnabled && (
            <Form.Item name="ssl_domain" label="SSL Certificate" rules={[{ required: true, message: 'Select an SSL certificate' }]}>
              <Select
                showSearch
                placeholder="Select uploaded SSL certificate"
                filterOption={(input, option) => (option?.label as string||'').toLowerCase().includes(input.toLowerCase())}
                options={sslDomains.map(d => ({ value: d.domain, label: d.domain }))}
                onChange={(domain) => {
                  const cert = sslDomains.find(d => d.domain === domain)
                  if (cert) {
                    setSelectedSslDomain(domain)
                    form.setFieldsValue({
                      fullchain: cert.fullchain_path,
                      privkey: cert.privkey_path,
                      domain: domain,
                    })
                  }
                }}
              />
            </Form.Item>
          )}
          <Form.Item name="fullchain" hidden><Input /></Form.Item>
          <Form.Item name="privkey" hidden><Input /></Form.Item>

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

          {/* ---- Auto Deploy / Missing Files LLM Generation ---- */}
          {checkingMissing ? (
            <div style={{padding:'8px 0'}}><Spin size="small" /> <span style={{fontSize:12,color:'#999'}}>Checking deployment readiness...</span></div>
          ) : checkError ? (
            <Alert type="error" message={`Deployment readiness check failed: ${checkError}`} style={{marginBottom:12}} />
          ) : missingFiles.length > 0 ? (
            <Alert
              type={Object.keys(generatedFiles).length > 0 ? 'success' : 'warning'}
              message={
                Object.keys(generatedFiles).length === 0
                  ? `Missing essential files: ${missingFiles.join(', ')}`
                  : undefined
              }
              description={
                <div style={{marginTop:4}}>
                  {Object.keys(generatedFiles).length === 0 ? (
                    <>
                      <Checkbox
                        checked={autoDeploy}
                        onChange={(e) => setAutoDeploy(e.target.checked)}
                      >
                        <strong>Auto Templates Completion</strong> — use BYOK LLM to generate missing files and deploy automatically
                      </Checkbox>
                      <div style={{marginTop:8}}>
                        <Button
                          size="small"
                          icon={<RobotOutlined />}
                          loading={generatingFiles}
                          onClick={generateMissingFiles}
                        >
                          Generate Missing Files via LLM
                        </Button>
                        <div style={{marginTop:4,fontSize:12,color:'#999'}}>
                          Generates only the missing files — review them before deploying. You can also click <strong>Deploy</strong> directly to auto-generate and deploy (Auto Templates Completion).
                        </div>
                      </div>
                    </>
                  ) : (
                    <>
                      <Tag color="green">✓ Generated {Object.keys(generatedFiles).length} file(s)</Tag>
                      <div style={{marginTop:8}}>
                        {Object.keys(generatedFiles).map(fn => (
                          <Tag key={fn} color="blue" style={{cursor:'pointer',marginTop:4}}
                            onClick={() => {
                              setEditorFileName(fn)
                              setEditorContent(String(generatedFiles[fn]))
                              setEditorModalOpen(true)
                            }}>
                            📄 {fn} — click to view
                          </Tag>
                        ))}
                      </div>
                      <div style={{marginTop:8,fontSize:12,color:'#666'}}>
                        Review the generated files (click to open in the editor). When ready, click <strong>Deploy</strong> to proceed — or edit the files first.
                      </div>
                    </>
                  )}
                </div>
              }
              style={{marginBottom:12}}
            />
          ) : selectedServiceName ? (
            <Alert type="success" message="All essential files present — ready to deploy" style={{marginBottom:12}} />
          ) : null}

          {/* ---- Generated File Editor Modal (Monaco) ---- */}
          <Modal
            title={<Space><Text strong>{editorFileName}</Text><Tag color="blue">LLM Generated</Tag></Space>}
            open={editorModalOpen}
            onCancel={() => setEditorModalOpen(false)}
            footer={
              <Space>
                <Button onClick={() => setEditorModalOpen(false)}>Close</Button>
                <Button type="primary" loading={editorSaving} onClick={saveEditorFile}>Save</Button>
              </Space>
            }
            width="85%"
          >
            <div style={{height:'60vh'}}>
              <Editor
                height="100%"
                language={getLanguage(editorFileName)}
                theme="vs-dark"
                value={editorContent}
                onChange={(val) => setEditorContent(val || '')}
                options={{
                  minimap: { enabled: false },
                  fontSize: 13,
                  wordWrap: 'on',
                  scrollBeyondLastLine: false,
                  automaticLayout: true,
                }}
              />
            </div>
          </Modal>

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
            <Button type="primary" htmlType="submit" loading={loading} icon={<span>🚀</span>}
              disabled={!selectedUserName || !selectedServiceName || (!autoDeploy && (!!checkError || (missingFiles.length > 0 && Object.keys(generatedFiles).length === 0)))}>
              Deploy
            </Button>
          </Space>
        </Space>
      </Form>
    </Modal>
  )
}
