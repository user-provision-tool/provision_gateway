import { useState, useEffect, useRef } from 'react'
import { Modal, Form, Input, Select, Button, Switch, Space, Divider, message, Checkbox, Alert, Spin, Tag, Typography } from 'antd'
import { PlusOutlined, MinusCircleOutlined, GlobalOutlined, RobotOutlined } from '@ant-design/icons'
import Editor from '@monaco-editor/react'
import client from '../../api/client'
import { deriveProfiles, getComposePreview } from '../../api/services'
import { useGenerationJob } from '../../hooks/useGenerationJob'
import FileSetSelection, {
  EMPTY_CANDIDATES,
  EMPTY_SELECTION,
  FileSetCandidates,
  FileSetSelectionData,
  FileSetStale,
} from './FileSetSelection'

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
  const [sources, setSources] = useState<{ name: string; files: string[]; has_compose_template?: boolean; recipes?: { path: string; label: string; is_root: boolean; template_files: string[] }[] }[]>([])
  const [deployableUsers, setDeployableUsers] = useState<{username:string,label:string}[]>([])
  const [sslDomains, setSslDomains] = useState<{domain:string, fullchain_path:string, privkey_path:string}[]>([])
  const [selectedSslDomain, setSelectedSslDomain] = useState<string>('')

  // Auto-deploy / missing files state
  const [checkingMissing, setCheckingMissing] = useState(false)
  const [missingFiles, setMissingFiles] = useState<string[]>([])
  const [generatingFiles, setGeneratingFiles] = useState(false)
  const [generatedFiles, setGeneratedFiles] = useState<Record<string,string>>({})

  // Base-file selection (design §Selection & UI — both panels show the full
  // selection + prompt UI; the stored file set is the pre-selection default).
  const [llmActive, setLlmActive] = useState(false)
  const [fileSetCandidates, setFileSetCandidates] = useState<FileSetCandidates>(EMPTY_CANDIDATES)
  const [fileSetStale, setFileSetStale] = useState<FileSetStale>({})
  const [selection, setSelection] = useState<FileSetSelectionData>(EMPTY_SELECTION)
  const [needsEnv, setNeedsEnv] = useState(true)
  const [prompt, setPrompt] = useState('')
  // Deploy-time per-user env generation (GAP-19): fresh SECRET_KEY per instance.
  const [perUserEnv, setPerUserEnv] = useState<string | null>(null)
  const { jobState: perUserJob, startJob: startPerUserJob, cancelJob: cancelPerUserJob, reset: resetPerUserJob } = useGenerationJob()
  // Editor modal for reviewing generated files (clickable → built-in editor)
  const [editorModalOpen, setEditorModalOpen] = useState(false)
  const [editorFileName, setEditorFileName] = useState('')
  const [editorContent, setEditorContent] = useState('')
  const [editorSaving, setEditorSaving] = useState(false)

  // GAP-003: Auto-computed next label state
  const [nextLabel, setNextLabel] = useState<string>('0')
  const [computingLabel, setComputingLabel] = useState(false)

  // Auto-detected volume keys from compose template
  const [expectedVolumeKeys, setExpectedVolumeKeys] = useState<string[]>([])

  // Helper: parse service value which may include recipe path (format: "name@@recipe_path")
  const parseServiceValue = (val: string): { baseName: string; recipePath: string } => {
    const idx = val.indexOf('@@')
    return idx >= 0 ? { baseName: val.slice(0, idx), recipePath: val.slice(idx + 2) } : { baseName: val, recipePath: '' }
  }

  // Fetch expected volumes from the converter's in-call src→key mapping via
  // the lightweight compose-preview response (design §Implementation notes
  // L284-286) — keys are NEVER parsed from .j2 templates in the frontend.
  const fetchExpectedVolumes = async (serviceName: string, sourcesList?: { name: string; files: string[] }[]) => {
    try {
      const svcVal = serviceName || ''
      const { baseName, recipePath } = parseServiceValue(svcVal)
      const list = sourcesList || sources
      const svc = list.find(s => s.name === baseName)
      if (!svc) return
      const recipePrefix = recipePath ? `${recipePath}/` : ''
      const recipeFiles = recipePrefix
        ? svc.files.filter((f: string) => f.startsWith(recipePrefix))
        : svc.files
      const pathFor = (f: string) => (recipePrefix ? f.slice(recipePrefix.length) : f)

      // Same preference order as the deploy payload: the selected compose set
      // wins; else plain compose files; else the .j2 template (the server
      // resolves it to its source or falls back server-side).
      let composeFiles: string[] = []
      if (selection.compose.length > 0) {
        composeFiles = selection.compose
      } else {
        const plain = recipeFiles.filter((f: string) =>
          (f.endsWith('.yml') || f.endsWith('.yaml')) && !f.endsWith('.j2'))
        if (plain.length > 0) {
          composeFiles = plain.map(pathFor)
        } else {
          const composeJ2 = recipeFiles.find((f: string) => f.endsWith('.yml.j2'))
          if (composeJ2) composeFiles = [pathFor(composeJ2)]
        }
      }
      if (composeFiles.length === 0) { setExpectedVolumeKeys([]); return }

      const { data } = await getComposePreview(baseName, composeFiles, recipePath)
      setExpectedVolumeKeys(data.volume_keys || [])
    } catch { setExpectedVolumeKeys([]) }
  }

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
      const init = async () => {
        const sourcesData = await loadSources()
        loadProxyStatus()
        loadDeployableUsers()
        loadSslDomains()
        // Reset auto-deploy state
        setMissingFiles([])
        setCheckError(null)
        setGeneratedFiles({})
        setEditorModalOpen(false)
        setNextLabel('0')
        // Selection + per-user env state
        setSelection(EMPTY_SELECTION)
        setFileSetCandidates(EMPTY_CANDIDATES)
        setFileSetStale({})
        setNeedsEnv(true)
        setPrompt('')
        setPerUserEnv(null)
        resetPerUserJob()
        try {
          const { data: cfg } = await client.get('/llm/config')
          setLlmActive(!!(cfg && (cfg.is_active || cfg.byok_configured)))
        } catch { setLlmActive(false) }
        // Preselect service if provided
        if (preselectedService) {
          form.setFieldsValue({ service_name: preselectedService })
          checkMissingFiles(preselectedService)
          fetchExpectedVolumes(preselectedService, sourcesData)
        }
      }
      init()
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
      return data.services || []
    } catch { return [] }
  }

  const loadProxyStatus = async () => {
    try {
      const { data } = await client.get('/system/proxy')
      setProxyEnabled(data.has_active === true)
    } catch { /* proxy not configured */ }
  }

  const [checkError, setCheckError] = useState<string | null>(null)

  // Load the stored file set + live candidates + stale markers for the
  // selected service/recipe (design §Selection & UI — dashboard pre-selection).
  const loadFileSets = async (baseName: string, recipePath: string) => {
    try {
      const { data } = await client.get(`/services/${baseName}/file-sets`)
      const rp = recipePath || '.'
      const entry = (data.file_sets || {})[rp] || {}
      setFileSetCandidates((data.candidates || {})[rp] || EMPTY_CANDIDATES)
      setFileSetStale((data.stale || {})[rp] || {})
      setSelection({
        compose: entry.compose || [],
        nginx: entry.nginx || null,
        env: entry.env || [],
        profiles: entry.profiles || [],
      })
      setPerUserEnv(null)
    } catch { /* selection stays empty on failure */ }
  }

  // Profiles recompute (GAP-2): profile candidates derive from the merged
  // compose — when the in-panel compose selection changes, refetch candidates
  // so the profiles section follows the selection (design §Selection & UI
  // L59-62) instead of staying stale from the stored file set.
  const watchedService = Form.useWatch('service_name', form)
  useEffect(() => {
    if (!open || !watchedService) return
    const { baseName, recipePath } = parseServiceValue(watchedService)
    let cancelled = false
    if (selection.compose.length === 0) {
      setFileSetCandidates(prev => ({ ...prev, profiles: [] }))
      return
    }
    deriveProfiles(baseName, selection.compose, recipePath)
      .then(({ data }) => {
        if (cancelled) return
        const profiles: string[] = (data.candidates && data.candidates.profiles) || []
        setFileSetCandidates(prev => ({ ...prev, profiles }))
        // A profile absent from the currently selected compose cannot be
        // activated — drop it from the selection (in-panel stale semantics).
        setSelection(prev => ({
          ...prev,
          profiles: prev.profiles.filter(p => profiles.includes(p)),
        }))
      })
      .catch(() => {
        if (!cancelled) setFileSetCandidates(prev => ({ ...prev, profiles: [] }))
      })
    return () => { cancelled = true }
  }, [open, watchedService, selection.compose])

  // Check for missing essential files when service selection changes
  const checkMissingFiles = async (serviceName: string) => {
    if (!serviceName) { setMissingFiles([]); return }
    setCheckingMissing(true)
    try {
      // Parse recipe path from service value (format: "name@@recipe_path")
      const [baseName, recipePath] = serviceName.includes('@@') ? serviceName.split('@@') : [serviceName, '']
      const params: Record<string, string> = {}
      if (recipePath) params.recipe_path = recipePath
      const { data } = await client.get(`/services/${baseName}/check-missing-files`, { params })
      setMissingFiles(data.missing || [])
      setCheckError(null)
      setNeedsEnv(data.needs_env !== false)
      loadFileSets(baseName, recipePath)
    } catch (err: any) {
      setCheckError(err?.message || 'Check failed — try again')
      setMissingFiles([])
    } finally { setCheckingMissing(false) }
  }

  // Selection used for generation/deploy requests — stale stored entries are
  // excluded (kept + flagged in the UI, never sent, design §Selection & UI).
  const effectiveSelection = (): FileSetSelectionData => {
    const st = fileSetStale || {}
    return {
      compose: selection.compose.filter(p => !(st.compose || []).includes(p)),
      nginx: selection.nginx && !(st.nginx || []).includes(selection.nginx) ? selection.nginx : null,
      env: selection.env.filter(p => !(st.env || []).includes(p)),
      profiles: selection.profiles.filter(p => !(st.profiles || []).includes(p)),
    }
  }

  // Async generation job (ordered phases compose → nginx → env, one job).
  const genJob = useGenerationJob()
  const [genValidations, setGenValidations] = useState<Record<string, any>>({})

  const generateMissingFiles = async () => {
    setGeneratingFiles(true)
    try {
      const svcVal = form.getFieldValue('service_name') || ''
      const { baseName, recipePath } = parseServiceValue(svcVal)
      await genJob.startJob({
        service_name: baseName,
        recipe_path: recipePath,
        job_type: 'generate_missing',
        prompt,
        selection: effectiveSelection(),
        deploy_metadata: {
          service_name: baseName,
          user_name: form.getFieldValue('user_name') || '',
          label: form.getFieldValue('label') || '0',
          domain: form.getFieldValue('domain') || 'localhost',
        },
      })
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to start generation job')
    } finally { setGeneratingFiles(false) }
  }

  // Generation job completed → move the result into the review gate.
  useEffect(() => {
    const st = genJob.jobState
    if (!st || st.status !== 'completed') return
    const files = (st.result && st.result.files) || {}
    if (Object.keys(files).length > 0) {
      setGeneratedFiles(files)
      setGenValidations((st.result && st.result.validations) || {})
      message.success(`LLM generated ${Object.keys(files).length} file(s) — review, then deploy`)
    } else {
      message.warning('LLM returned no files. See the job result in the panel.')
    }
    genJob.reset()
  }, [genJob.jobState])

  // Deploy-time per-user env (GAP-19): writes {recipe}/.env.{user}.{label}
  // with a fresh SECRET_KEY; the stored default selection is untouched.
  const generatePerUserEnv = async () => {
    const svcVal = form.getFieldValue('service_name') || ''
    const { baseName, recipePath } = parseServiceValue(svcVal)
    const userName = form.getFieldValue('user_name')
    const label = form.getFieldValue('label') || '0'
    if (!userName) { message.warning('Select a user first'); return }
    try {
      await startPerUserJob({
        service_name: baseName,
        recipe_path: recipePath,
        job_type: 'per_user_env',
        prompt,
        selection: effectiveSelection(),
        user_name: userName,
        label,
        domain: form.getFieldValue('domain') || 'localhost',
        deploy_metadata: {
          service_name: baseName, user_name: userName, label,
          domain: form.getFieldValue('domain') || 'localhost',
        },
      })
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to start per-user env generation')
    }
  }

  useEffect(() => {
    const st = perUserJob
    if (!st || st.status !== 'completed') return
    const name = (st.result && st.result.per_user_env_name) || ''
    if (name) {
      setPerUserEnv(name)
      message.success(`Generated ${name} — this deployment will use it as its interpolation env`)
    } else {
      message.warning('Per-user env job completed without a file name')
    }
    resetPerUserJob()
  }, [perUserJob])

  // Save an edited generated file back to state + disk
  const saveEditorFile = async () => {
    if (!editorFileName) return
    setEditorSaving(true)
    try {
      // Merge the edited content into the generated-files state
      setGeneratedFiles(prev => ({ ...prev, [editorFileName]: editorContent }))
      // Persist to the service project on disk
      const svcVal = form.getFieldValue('service_name') || ''
      const { baseName, recipePath } = parseServiceValue(svcVal)
      await client.post('/services/save-generated', {
        service_name: baseName,
        recipe_path: recipePath,
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
      const gen = generatedFiles
      const sel = effectiveSelection()
      const hasSelectedCompose = sel.compose.length > 0

      // Compose is the root of the dependency graph: deploy hard-gates on a
      // compose source only. The implicit LLM auto-generation on deploy is
      // RETIRED — the operator generates via the panel, or selects a compose
      // file below.
      if (missingFiles.length > 0 && Object.keys(generatedFiles).length === 0 && !hasSelectedCompose) {
        message.error('Cannot deploy: missing essential files. Open the generate-missing panel or select a compose file below.')
        setLoading(false)
        return
      }

      const svcVal = values.service_name || ''
      const { baseName, recipePath } = parseServiceValue(svcVal)
      const selectedService = sources.find(s => s.name === baseName)

      // Save any LLM-generated files first
      if (Object.keys(gen).length > 0) {
        await client.post('/services/save-generated', {
          service_name: baseName,
          recipe_path: recipePath,
          files: gen,
          selection: sel,
        })
      }

      // Build deploy payload
      const payload: any = {
        user_name: values.user_name,
        service_name: baseName,
        project_root: recipePath ? `${baseName}/${recipePath}` : baseName,
        label: values.label || '0',
        domain: values.domain || 'localhost',
        passwd: values.passwd || '',
        https: values.https || false,
        use_global_proxy: values.use_global_proxy || false,
        // The selection is persisted as the new default on 202 accept
        // (server-side put_file_set) — design §Selection & UI L43-46.
        selection: sel,
      }

      // Add compose/nginx paths — the SELECTED set wins (multi, ordered);
      // legacy single-path convention detection applies only when nothing is
      // selected.
      if (sel.compose.length > 0) {
        payload.compose_file_paths = sel.compose
      } else if (selectedService) {
        // For a multi-recipe service, scope file lookup to the SELECTED recipe
        // subdirectory. project_root already points into the recipe dir, so the
        // template paths sent to the API are the bare filenames within it.
        const recipePrefix = recipePath ? `${recipePath}/` : ''
        const recipeFiles = recipePrefix
          ? selectedService.files.filter((f: string) => f.startsWith(recipePrefix))
          : selectedService.files
        const pathFor = (f: string) => (recipePrefix ? f.slice(recipePrefix.length) : f)

        const composeJ2 = recipeFiles.find((f: string) => f.endsWith('.yml.j2'))
        const nginxJ2 = recipeFiles.find((f: string) => f.endsWith('.conf.j2'))
        // Fallback: look for regular (non-.j2) compose/nginx files that already exist in the project
        const composeFile = recipeFiles.find((f: string) =>
          (f.endsWith('.yml') || f.endsWith('.yaml')) && !f.endsWith('.j2'))
        const nginxFile = recipeFiles.find((f: string) =>
          f.endsWith('.conf') && !f.endsWith('.j2'))

        if (composeJ2) payload.compose_template_path = pathFor(composeJ2)
        else if (gen['docker-compose.yml']) payload.compose_file_path = 'docker-compose.yml'
        else if (composeFile) payload.compose_file_path = pathFor(composeFile)

        if (nginxJ2) payload.nginx_conf_template_path = pathFor(nginxJ2)
        else if (gen['nginx.conf']) payload.nginx_conf_file_path = 'nginx.conf'
        else if (nginxFile) payload.nginx_conf_file_path = pathFor(nginxFile)
      }

      // Repeatable profiles (later wins is the compose CLI behavior).
      if (sel.profiles.length > 0) payload.profiles = sel.profiles

      // Interpolation env: selected env files + the deploy-time per-user env.
      const envList: string[] = [...sel.env]
      if (perUserEnv && !envList.includes(perUserEnv)) envList.push(perUserEnv)
      if (envList.length > 0) {
        payload.env_files = envList
      } else if (selectedService && sel.compose.length === 0) {
        // Legacy auto-detect fallback (nothing selected).
        const recipePrefix = recipePath ? `${recipePath}/` : ''
        const recipeFiles = recipePrefix
          ? selectedService.files.filter((f: string) => f.startsWith(recipePrefix))
          : selectedService.files
        const envFile = recipeFiles.find((f: string) =>
          f === '.env' || f.endsWith('/.env'))
        if (envFile) payload.env_file_path = '.env'
        else if (gen['.env']) payload.env_file_path = '.env'
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
      onDeployed(data.task_id, values.user_name, baseName, values.label || '0')
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
  const selectedLabel = Form.useWatch('label', form)

  // Ref to access Form.List's add() function from outside the render
  const volumeAddRef = useRef<((defaultValue?: any) => void) | null>(null)

  // Auto-fill volume mapping when service+user+label selected
  useEffect(() => {
    if (!selectedServiceName || !selectedUserName) return
    // Parse recipe path from service value (format: "name@@recipe_path")
    const serviceBaseName = selectedServiceName.includes('@@') ? selectedServiceName.split('@@')[0] : selectedServiceName
    const label = selectedLabel || '0'
    const keys = expectedVolumeKeys
    if (keys.length === 0) return
    const volumes = keys.map(key => ({
      key,
      value: `/srv/provision/user_data/${selectedUserName}/${serviceBaseName}/${label}/${key}`
    }))
    // First set the values on the form
    form.setFieldsValue({ volumes })
    // Also ensure Form.List has the right number of items
    if (volumeAddRef.current) {
      const currentVolumes = form.getFieldValue('volumes') || []
      while (currentVolumes.length < keys.length) {
        volumeAddRef.current()
        currentVolumes.push({})
      }
      // After adding, set values again
      setTimeout(() => form.setFieldsValue({ volumes }), 0)
    }
  }, [selectedServiceName, selectedUserName, selectedLabel, expectedVolumeKeys])

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
                  const svc = form.getFieldValue('service_name') || ''
                  const { baseName: svcBase } = parseServiceValue(svc)
                  if (val && svcBase) computeNextLabel(val, svcBase)
                }}
              />
            </Form.Item>
            <Form.Item name="service_name" label="Service" rules={[{ required: true, message: 'Required' }]} style={{ flex: 1 }}>
              <Select
                showSearch
                placeholder="Select source project"
                disabled={!!preselectedService}
                options={(() => {
                  const opts: { value: string; label: string }[] = []
                  for (const s of sources) {
                    const recipes = s.recipes || []
                    if (recipes.length > 1) {
                      for (const r of recipes) {
                        const suffix = r.is_root ? '' : ` @ ${r.path}`
                        opts.push({ value: `${s.name}${r.is_root ? '' : '@@' + r.path}`, label: `${s.name}${suffix}` })
                      }
                    } else {
                      opts.push({ value: s.name, label: s.name })
                    }
                  }
                  return opts
                })()}
                onChange={(val) => {
                  checkMissingFiles(val)
                  fetchExpectedVolumes(val)
                  // Parse base name for label computation (strip @@recipe_path)
                  const baseName = val.includes('@@') ? val.split('@@')[0] : val
                  const user = form.getFieldValue('user_name')
                  if (val && user) computeNextLabel(user, baseName)
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
            {(fields, { add, remove }) => {
              volumeAddRef.current = add
              return (
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
              )
            }}
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

          {/* ---- Base file selection (design §Selection & UI) ---- */}
          <Divider plain>Base file selection <span style={{fontSize:12,color:'#999'}}>— stored default pre-selected; save happens on deploy</span></Divider>
          <FileSetSelection
            selection={selection}
            candidates={fileSetCandidates}
            stale={fileSetStale}
            needsEnv={needsEnv}
            onChange={setSelection}
          />

          {/* ---- Deploy-time per-user env (GAP-19) ---- */}
          {needsEnv && (
            <Space direction="vertical" style={{ width: '100%' }} size={4}>
              <Divider plain style={{ margin: '4px 0' }}>Deploy-time per-user env (fresh SECRET_KEY per instance)</Divider>
              {perUserEnv ? (
                <Alert type="success" showIcon message={`Per-user env ready: ${perUserEnv} — this deployment will use it as its interpolation env`} />
              ) : (
                <>
                  {perUserJob && (perUserJob.status === 'queued' || perUserJob.status === 'running') && (
                    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                      <Spin size="small" />
                      <Text type="secondary" style={{ fontSize: 12 }}>Generating per-user env… {perUserJob.progress || ''}</Text>
                      <Button size="small" danger onClick={cancelPerUserJob}>Cancel</Button>
                    </div>
                  )}
                  {perUserJob && (perUserJob.status === 'failed' || perUserJob.status === 'cancelled') && (
                    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                      <Text type="danger" style={{ fontSize: 12 }}>{perUserJob.error || `Job ${perUserJob.status}`}</Text>
                      <Button size="small" onClick={resetPerUserJob}>Dismiss</Button>
                    </div>
                  )}
                  <Button
                    size="small"
                    icon={<RobotOutlined />}
                    disabled={!llmActive || (!!perUserJob && (perUserJob.status === 'queued' || perUserJob.status === 'running'))}
                    onClick={generatePerUserEnv}
                  >
                    Generate per-user .env at deploy time
                  </Button>
                  <div style={{ fontSize: 12, color: '#999' }}>
                    Uses the prompt below + user/label/domain to produce a fresh SECRET_KEY for this instance only — the stored default env selection is untouched.
                  </div>
                </>
              )}
            </Space>
          )}

          {/* ---- Generation prompt (exists in BOTH panels) ---- */}
          <Divider plain>Generation prompt</Divider>
          <Input.TextArea
            rows={2}
            disabled={!llmActive}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Prompt for the generation agent (missing-files generation + deploy-time per-user env). You may include doc URLs — the agent fetches them."
          />

          {/* ---- Missing Files / LLM Generation ---- */}
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
                      <div style={{marginTop:8}}>
                        <Button
                          size="small"
                          icon={<RobotOutlined />}
                          loading={generatingFiles}
                          disabled={!llmActive}
                          onClick={generateMissingFiles}
                        >
                          Generate Missing Files via LLM
                        </Button>
                        {!llmActive && (
                          <div style={{marginTop:4,fontSize:12,color:'#999'}}>
                            No LLM configured — no-LLM fallback strategy in effect: selection/manual flows remain usable; deploy is allowed with a warning.
                          </div>
                        )}
                        <div style={{marginTop:4,fontSize:12,color:'#999'}}>
                          Runs the ordered phases (compose → nginx → env) as one async job with progress polling; generated files appear below for review before deploy.
                        </div>
                        {genJob.jobState && (genJob.jobState.status === 'queued' || genJob.jobState.status === 'running') && (
                          <div style={{marginTop:8, display:'flex', gap: 8, alignItems:'center'}}>
                            <Spin size="small" />
                            <Text type="secondary" style={{fontSize:12}}>Generating… {genJob.jobState.progress || ''}</Text>
                            <Button size="small" danger onClick={genJob.cancelJob}>Cancel</Button>
                          </div>
                        )}
                        {genJob.jobState && (genJob.jobState.status === 'failed' || genJob.jobState.status === 'cancelled') && (
                          <div style={{marginTop:8}}>
                            <Text type="danger" style={{fontSize:12}}>{genJob.jobState.error || `Job ${genJob.jobState.status}`}</Text>
                            <Button size="small" style={{marginLeft:8}} onClick={genJob.reset}>Dismiss</Button>
                          </div>
                        )}
                      </div>
                    </>
                  ) : (
                    <>
                      <Tag color="green">✓ Generated {Object.keys(generatedFiles).length} file(s)</Tag>
                      <div style={{marginTop:8}}>
                        {Object.keys(generatedFiles).map(fn => {
                          const phase = fn === 'docker-compose.yml' ? 'compose' : fn === 'nginx.conf' ? 'nginx' : fn.startsWith('.env') ? 'env' : ''
                          const v = (genValidations || {})[phase]
                          const ok = !v || v.valid === true
                          return (
                            <div key={fn} style={{marginTop:4, display:'flex', alignItems:'center', gap: 8}}>
                              <Tag color={ok ? 'green' : 'red'} style={{margin:0}}>{ok ? '✓' : '✗'}</Tag>
                              <Tag color="blue" style={{cursor:'pointer', margin:0}}
                                onClick={() => {
                                  setEditorFileName(fn)
                                  setEditorContent(String(generatedFiles[fn]))
                                  setEditorModalOpen(true)
                                }}>
                                📄 {fn} — click to view/edit
                              </Tag>
                              {!ok && (
                                <Text type="danger" style={{fontSize:11}}>
                                  {((v && v.errors) || []).join('; ').slice(0, 160)}
                                </Text>
                              )}
                            </div>
                          )
                        })}
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
              disabled={!selectedUserName || !selectedServiceName || !!checkError || (missingFiles.length > 0 && Object.keys(generatedFiles).length === 0 && effectiveSelection().compose.length === 0)}>
              Deploy
            </Button>
          </Space>
        </Space>
      </Form>
    </Modal>
  )
}
