import { useState, useEffect, useRef } from 'react'
import { Modal, Form, Input, Select, Button, Switch, Space, Divider, message, Checkbox, Alert, Spin, Tag, Typography, Collapse } from 'antd'
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
  // True while the stored file-set selection for the chosen service/recipe is
  // still being fetched — Deploy must not run with an unresolved selection.
  const [fileSetLoading, setFileSetLoading] = useState(false)
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
  // Canonical per-user volume root (from compose-preview; never hardcoded —
  // the provision-api bootstraps bind targets under ITS user_data root).
  const [userDataDir, setUserDataDir] = useState<string>('/srv/provision/user_data')

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
      // Reset stale volume rows whenever the service changes — the mapping
      // is user+service+label-dependent and must never survive a selection
      // change (design §8.2).
      setExpectedVolumeKeys([])
      form.setFieldsValue({ volumes: [] })
      const { baseName, recipePath } = parseServiceValue(svcVal)
      const list = sourcesList || sources
      const svc = list.find(s => s.name === baseName)
      if (!svc) return
      const recipePrefix = recipePath ? `${recipePath}/` : ''
      const recipeFiles = recipePrefix
        ? svc.files.filter((f: string) => f.startsWith(recipePrefix))
        : svc.files
      // Exclude GENERATED artifacts (deploy-saved files have a sibling
      // `<name>.generated` marker) — merging a generated docker-compose.yml
      // into the preview changes the volume-key set and breaks the deploy
      // (the template still references every key of the real compose).
      const isGenerated = (f: string) => recipeFiles.includes(`${f}.generated`)
      const candidates = recipeFiles.filter((f: string) => !isGenerated(f))
      const pathFor = (f: string) => (recipePrefix ? f.slice(recipePrefix.length) : f)

      // Same preference order as the deploy payload: the selected compose set
      // wins; else plain compose files; else the .j2 template (the server
      // resolves it to its source or falls back server-side).
      let composeFiles: string[] = []
      if (selection.compose.length > 0) {
        composeFiles = selection.compose
      } else {
        const plain = candidates.filter((f: string) =>
          (f.endsWith('.yml') || f.endsWith('.yaml')) && !f.endsWith('.j2'))
        if (plain.length > 0) {
          composeFiles = plain.map(pathFor)
        } else {
          const composeJ2 = candidates.find((f: string) => f.endsWith('.yml.j2'))
          if (composeJ2) composeFiles = [pathFor(composeJ2)]
        }
      }
      if (composeFiles.length === 0) { setExpectedVolumeKeys([]); return }

      const { data } = await getComposePreview(baseName, composeFiles, recipePath)
      setExpectedVolumeKeys(data.volume_keys || [])
      if (data.user_data_dir) setUserDataDir(data.user_data_dir)
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
      const { data } = await client.get(`/services/${userName}/${serviceName}/next-label`)
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
      const { data } = await client.get('/projects')
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
    setFileSetLoading(true)
    try {
      const { data } = await client.get(`/projects/${baseName}/file-sets`)
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
    finally { setFileSetLoading(false) }
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
      const { data } = await client.get(`/projects/${baseName}/check-missing-files`, { params })
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
  // Deploy progress popup (design §8.3) — shows generation steps + deploy step.
  const [genPopupOpen, setGenPopupOpen] = useState(false)
  // True while handleDeploy's one-shot auto-gen runs — the popup FREEZES on the
  // completed frame (no reset, no "review, then deploy" toast) until /users/deploy
  // returns; the manual Generate button keeps the review-gate behavior (feature 4).
  const deployAutoRunRef = useRef(false)

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
        user_name: form.getFieldValue('user_name') || '',
        label: form.getFieldValue('label') || '0',
        deploy_metadata: {
          service_name: baseName,
          user_name: form.getFieldValue('user_name') || '',
          label: form.getFieldValue('label') || '0',
          domain: form.getFieldValue('domain') || 'localhost',
          https: !!form.getFieldValue('https'),
        },
      })
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to start generation job')
    } finally { setGeneratingFiles(false) }
  }

  // Deploy-path auto-generation: run the pipeline and WAIT for completion,
  // then adopt the generated per-user env name (design §2 Deploy auto-run).
  // `snap` = the form values captured BEFORE the panel was closed (the panel is
  // destroyOnClose/preserve=false, so the live form is gone by now).
  const generateMissingFilesForDeploy = async (snap: any) => {
    const svcVal = snap.service_name || ''
    const { baseName, recipePath } = parseServiceValue(svcVal)
    const jobId = await genJob.startJob({
      service_name: baseName,
      recipe_path: recipePath,
      job_type: 'generate_missing',
      prompt,
      selection: effectiveSelection(),
      user_name: snap.user_name || '',
      label: snap.label || '0',
      deploy_metadata: {
        service_name: baseName,
        user_name: snap.user_name || '',
        label: snap.label || '0',
        domain: snap.domain || 'localhost',
        https: !!snap.https,
      },
    })
    if (!jobId) throw new Error('Failed to start generation job')
    for (let i = 0; i < 900; i++) {
      await new Promise(r => setTimeout(r, 1000))
      const { data } = await client.get(`/llm/jobs/${jobId}`)
      if (data.status === 'completed') {
        const result = data.result || {}
        const name = result.env_name || null
        const files = result.files || {}
        if (name) setPerUserEnv(name)
        if (Object.keys(files).length > 0) setGeneratedFiles(files)
        // Return the fresh results synchronously — React state updates are not
        // visible inside this still-running async flow, and the deploy payload
        // must reference the just-generated env.raw name + files NOW.
        return { envName: name, files }
      }
      if (data.status === 'failed' || data.status === 'cancelled') {
        throw new Error(data.error || `Generation ${data.status}`)
      }
    }
    throw new Error('Generation timed out')
  }

  // Generation job completed → move the result into the review gate. On the
  // Deploy auto-run path (popup open) the completed events are KEPT so the popup
  // freezes on the final frame; the manual Generate path resets into the review
  // gate with the "review, then deploy" toast (feature 4).
  useEffect(() => {
    const st = genJob.jobState
    if (!st || st.status !== 'completed') return
    const isDeployAuto = deployAutoRunRef.current
    const files = (st.result && st.result.files) || {}
    if (Object.keys(files).length > 0) {
      setGeneratedFiles(files)
      setGenValidations((st.result && st.result.validations) || {})
      if (!isDeployAuto) {
        message.success(`LLM generated ${Object.keys(files).length} file(s) — review, then deploy`)
      }
    } else if (!isDeployAuto) {
      message.warning('LLM returned no files. See the job result in the panel.')
    }
    const name = (st.result && st.result.env_name) || null
    if (name) setPerUserEnv(name)
    if (!isDeployAuto) genJob.reset()
  }, [genJob.jobState])

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
      await client.post('/projects/save-generated', {
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
    setGenPopupOpen(true)
    deployAutoRunRef.current = true
    // Snapshot the form values BEFORE closing the panel: the modal is
    // destroyOnClose + the Form is preserve={false}, so once the panel is gone
    // every `form.getFieldValue` below would read empty.
    const snap: any = { ...values }
    // Close the deploy panel immediately — the Deploying… popup is the UI from
    // here on (it lives in this component, which stays mounted).
    onClose()
    try {
      // Snapshot previously generated files; the Deploy auto-run below adds
      // anything generated during THIS call and returns it synchronously
      // (React state is not visible mid-async-flow).
      const sel = effectiveSelection()
      const hasSelectedCompose = sel.compose.length > 0

      const svcVal = snap.service_name || ''
      const { baseName, recipePath } = parseServiceValue(svcVal)
      const selectedService = sources.find(s => s.name === baseName)

      // ---- Always-on generation (design §2): Deploy auto-runs whatever is
      // missing (compose/nginx + the per-user env.raw) BEFORE deploying —
      // no review pause on the Deploy path. ----
      const needsGeneration =
        missingFiles.length > 0 ||
        (needsEnv && !perUserEnv)
      if (needsGeneration && !hasSelectedCompose && missingFiles.length > 0 && !genJob.jobState) {
        message.error('Cannot deploy: missing essential files and no compose selected.')
        setLoading(false)
        setGenPopupOpen(false)
        deployAutoRunRef.current = false
        return
      }
      let envName = perUserEnv
      let genNow: Record<string, string> = {}
      if (needsGeneration) {
        const res = await generateMissingFilesForDeploy(snap)
        envName = (res && res.envName) || perUserEnv
        genNow = (res && res.files) || {}
      }
      // Merge previously generated (reviewed) files with the just-generated
      // ones — the Deploy auto-run MUST see them (compose/nginx are persisted
      // for the -api render; re-persisting the env.raw identically is a no-op).
      const gen = { ...generatedFiles, ...genNow }

      // Save any LLM-generated files first (also persists the selection as the
      // new stored default — design §Selection & UI L43-46).
      if (Object.keys(gen).length > 0) {
        await client.post('/projects/save-generated', {
          service_name: baseName,
          recipe_path: recipePath,
          files: gen,
          selection: sel,
        })
      }

      // Build deploy payload
      const payload: any = {
        user_name: snap.user_name,
        service_name: baseName,
        project_root: recipePath ? `${baseName}/${recipePath}` : baseName,
        label: snap.label || '0',
        domain: snap.domain || 'localhost',
        passwd: snap.passwd || '',
        https: snap.https || false,
        use_global_proxy: snap.use_global_proxy || false,
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
        // Fallback: look for regular (non-.j2) compose files that already exist in the project
        const composeFile = recipeFiles.find((f: string) =>
          (f.endsWith('.yml') || f.endsWith('.yaml')) && !f.endsWith('.j2'))

        if (composeJ2) payload.compose_template_path = pathFor(composeJ2)
        else if (gen['docker-compose.yml']) payload.compose_file_path = 'docker-compose.yml'
        else if (composeFile) payload.compose_file_path = pathFor(composeFile)
      }

      // Nginx wiring applies in BOTH cases above: a selected compose set does
      // NOT make nginx irrelevant. Without a per-user nginx conf the shared
      // nginx has no route for the service and the edge 502s the /go/ relay.
      // Preference: panel selection > LLM-generated nginx.conf (saved to the
      // recipe by save-generated above) > recipe .conf.j2 template > plain
      // recipe .conf.
      {
        const recipePrefixN = recipePath ? `${recipePath}/` : ''
        const recipeFilesN = recipePrefixN
          ? (selectedService?.files || []).filter((f: string) => f.startsWith(recipePrefixN))
          : (selectedService?.files || [])
        const pathForN = (f: string) => (recipePrefixN ? f.slice(recipePrefixN.length) : f)
        const nginxJ2 = recipeFilesN.find((f: string) => f.endsWith('.conf.j2'))
        const nginxFile = recipeFilesN.find((f: string) =>
          f.endsWith('.conf') && !f.endsWith('.j2') && !f.endsWith('.generated'))
        if (sel.nginx) {
          payload.nginx_conf_file_path = sel.nginx
        } else if (gen['nginx.conf']) {
          payload.nginx_conf_file_path = 'nginx.conf'
        } else if (nginxJ2) {
          payload.nginx_conf_template_path = pathForN(nginxJ2)
        } else if (nginxFile) {
          payload.nginx_conf_file_path = pathForN(nginxFile)
        }
      }

      // Repeatable profiles (later wins is the compose CLI behavior).
      if (sel.profiles.length > 0) payload.profiles = sel.profiles

      // Interpolation env: ONLY the generated per-user env.raw is passed to
      // the -api (design §2 — selection is generation context, never the
      // handoff). Empty list when nothing was generated.
      if (envName) {
        payload.env_files = [envName]
      }

      // HTTPS certs
      if (snap.https) {
        payload.fullchain = snap.fullchain || ''
        payload.privkey = snap.privkey || ''
      }

      // Volume mapping
      if (snap.volumes && snap.volumes.length > 0) {
        payload.volumes = {}
        for (const v of snap.volumes) {
          if (v.key && v.value) payload.volumes[v.key] = v.value
        }
      }

      // Build args
      if (snap.build_args && snap.build_args.length > 0) {
        payload.build_args = {}
        for (const a of snap.build_args) {
          if (a.key && a.value) payload.build_args[a.key] = a.value
        }
      }

      const { data } = await client.post('/services/deploy', payload)
      message.success(`Deploy queued! Task: ${data.task_id}`)
      onDeployed(data.task_id, snap.user_name, baseName, snap.label || '0')
      onClose()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Deploy failed')
    } finally {
      setLoading(false)
      setGenPopupOpen(false)
      deployAutoRunRef.current = false
      // Free the job state now that the popup is closed (feature 4: during the
      // auto-run the completed frame was intentionally kept in the popup).
      genJob.reset()
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
      value: `${userDataDir}/${selectedUserName}/${serviceBaseName}/${label}/${key}`
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
  }, [selectedServiceName, selectedUserName, selectedLabel, expectedVolumeKeys, userDataDir])

  return (
    <>
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
        
        {/* Hidden certificate fields live OUTSIDE the Space: as direct Space
            children they rendered zero-height rows, each adding a 16px gap
            (the "3 blank lines" above Volume Mapping). */}
        <Form.Item name="fullchain" hidden><Input /></Form.Item>
        <Form.Item name="privkey" hidden><Input /></Form.Item>
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
                    if (recipes.length === 0) {
                      // No configured recipe dirs → the project root itself.
                      opts.push({ value: s.name, label: s.name })
                      continue
                    }
                    // ONE option per configured recipe — including the
                    // single-recipe case. Gating this on `length > 1` meant a
                    // project with exactly one sub-recipe (e.g. dify → docker)
                    // lost its path: candidates were then looked up under '.'
                    // (empty) and the Compose selector stayed disabled, so a
                    // freshly imported monorepo could not be deployed at all.
                    for (const r of recipes) {
                      const suffix = r.is_root ? '' : ` @ ${r.path}`
                      opts.push({ value: `${s.name}${r.is_root ? '' : '@@' + r.path}`, label: `${s.name}${suffix}` })
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
          {/* Label + toggle on ONE line, no bottom margin (was a vertical
              label row + switch row + 24px gap). */}
          <Form.Item style={{ marginBottom: 0 }}>
            <Space size={8} align="center">
              <Text>Enable HTTPS</Text>
              <Form.Item name="https" valuePropName="checked" noStyle>
                <Switch onChange={(v) => {
                  setHttpsEnabled(v)
                  if (!v) {
                    setSelectedSslDomain('')
                    form.setFieldsValue({ ssl_domain: undefined, fullchain: '', privkey: '' })
                  }
                }} />
              </Form.Item>
            </Space>
          </Form.Item>
          {httpsEnabled && (
            <Form.Item name="ssl_domain" label="SSL Certificate" style={{ marginTop: 12, marginBottom: 0 }}
              rules={[{ required: true, message: 'Select an SSL certificate' }]}>
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
          {/* Exactly ONE blank line above Volume Mapping: the hidden cert fields
              now live outside this Space (they used to add zero-height Space
              rows), so the Space's own 16px gap is the single separator. */}
          <Collapse
            size="small"
            ghost
            items={[
              {
                key: 'volumes',
                label: 'Volume Mapping (optional)',
                children: (
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
                ),
              },
              {
                key: 'build_args',
                label: 'Build Args (optional)',
                children: (
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
                ),
              },
            ]}
          />

          {/* ---- Base file selection (design §Selection & UI) ---- */}
          <Divider plain>Base file selection <span style={{fontSize:12,color:'#999'}}>— stored default pre-selected; save happens on deploy</span></Divider>
          <FileSetSelection
            selection={selection}
            candidates={fileSetCandidates}
            stale={fileSetStale}
            needsEnv={needsEnv}
            onChange={setSelection}
          />

          {/* ---- Deploy-time per-user env (pipeline env phase, design §2) ---- */}
          {needsEnv && (
            <Space direction="vertical" style={{ width: '100%' }} size={4}>
              {perUserEnv ? (
                <Alert type="success" showIcon message={`Per-user env ready: ${perUserEnv} — this deployment will use it as its interpolation env`} />
              ) : (
                <div style={{ fontSize: 12, color: '#999' }}>
                  The per-user env is generated automatically as the last step of the missing-files pipeline when you Deploy (or click Generate).
                </div>
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
                          <b>Optional preview</b> — runs the ordered phases (compose → nginx → env) and pauses for your review before deploying. Clicking Deploy directly runs the same generation automatically but skips the review chance.
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
            {fileSetLoading && (
              <Text type="secondary" style={{ fontSize: 12 }}><Spin size="small" /> Loading file selection…</Text>
            )}
            {!fileSetLoading && (fileSetCandidates.compose || []).length > 0 && effectiveSelection().compose.length === 0 && (
              <Text type="warning" style={{ fontSize: 12 }}>
                Select at least one compose file — deploying with an empty selection skips env generation.
              </Text>
            )}
            {/* A compose may declare profile-gated services (e.g. the database or
                vector store). With none activated, `docker compose` starts only the
                unprofiled ones — the stack comes up missing those services and
                nothing errors, so say so before the operator deploys. Warning, not
                a block: an empty selection is legitimate for recipes whose optional
                services are genuinely not wanted. */}
            {!fileSetLoading && (fileSetCandidates.profiles || []).length > 0 && effectiveSelection().profiles.length === 0 && (
              <Text type="warning" style={{ fontSize: 12 }}>
                This compose gates {(fileSetCandidates.profiles || []).length} optional service profile(s) and none is
                selected — those services will not start. Select the ones this deployment needs
                (a database or vector store is usually required).
              </Text>
            )}
            <Button type="primary" htmlType="submit" loading={loading} icon={<span>🚀</span>}
              disabled={!selectedUserName || !selectedServiceName || !!checkError || fileSetLoading
                || (!fileSetLoading && (fileSetCandidates.compose || []).length > 0 && effectiveSelection().compose.length === 0)
                || (missingFiles.length > 0 && Object.keys(generatedFiles).length === 0 && effectiveSelection().compose.length === 0)}>
              Deploy
            </Button>
          </Space>
        </Space>
      </Form>
    </Modal>

    {/* ---- Deploy progress popup (design §8.3) — closeable anytime ---- */}
    <Modal
      title="Deploying…"
      open={genPopupOpen}
      onCancel={() => setGenPopupOpen(false)}
      footer={null}
      width={560}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={6}>
        {genJob.jobState && (genJob.jobState.status === 'queued' || genJob.jobState.status === 'running' || genJob.jobState.status === 'completed') && (
          <>
            {genJob.jobState.status === 'completed' ? (
              <Text style={{ fontSize: 13, color: '#389e0d' }}>
                Generation complete — deploying the instance…
              </Text>
            ) : (
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <Spin size="small" />
                <Text style={{ fontSize: 13 }}>{genJob.jobState.progress || 'Generating…'}</Text>
              </div>
            )}
            {(() => {
              // Append-only step list: one row per step (keyed by file), first
              // seen order preserved, the row UPDATED in place as the step
              // progresses — finished steps are never removed. Each row shows
              // the step's purpose and its in/out/rsn token counters.
              const evs = (genJob.jobState && genJob.jobState.events) || []
              const rows: any[] = []
              const at: Record<string, number> = {}
              for (const ev of evs) {
                // One row PER STEP (step id, falling back to file for the
                // terminal artifact rows) — steps accumulate, they are never
                // merged/overwritten, and each shows the token usage known at
                // the time it was emitted.
                const k = ev.step || ev.file || '?'
                if (at[k] === undefined) { at[k] = rows.length; rows.push({ ...ev }) }
                else { rows[at[k]] = { ...ev } }
              }
              return rows.map((ev, i) => (
                <div key={i} style={{ fontFamily: 'monospace', fontSize: 12, color: ev.status === 'done' ? '#389e0d' : ev.status === 'failed' ? '#cf1322' : ev.status === 'skipped (no ${VAR} interpolation)' ? '#8c8c8c' : '#1677ff' }}>
                  {ev.purpose ? <span style={{ color: '#8c8c8c' }}>{ev.purpose} · </span> : null}
                  {ev.file}  {ev.status}{ev.usage ? `  in ${(ev.usage.prompt/1000).toFixed(1)}k / out ${(ev.usage.completion/1000).toFixed(1)}k / rsn ${(ev.usage.reasoning/1000).toFixed(1)}k` : ''}
                </div>
              ))
            })()}
          </>
        )}
        {genJob.jobState && genJob.jobState.status === 'failed' && (
          <Text type="danger" style={{ fontSize: 13 }}>{genJob.jobState.error || 'Generation failed'}</Text>
        )}
        <div style={{ fontSize: 12, color: '#999' }}>
          You can close this popup anytime — the job continues in the background and remains visible on the Tasks page.
        </div>
      </Space>
    </Modal>
    </>
  )
}
