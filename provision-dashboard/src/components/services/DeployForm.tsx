import { useState, useEffect, useRef } from 'react'
import { Modal, Form, Input, Select, Button, Switch, Space, Divider, message, Checkbox, Alert, Spin, Tag, Typography, Collapse } from 'antd'
import { PlusOutlined, MinusCircleOutlined, GlobalOutlined, RobotOutlined } from '@ant-design/icons'
import Editor from '@monaco-editor/react'
import client from '../../api/client'
import { deriveProfiles, getComposePreview } from '../../api/services'
import { useGenerationJob, isPaused } from '../../hooks/useGenerationJob'
import ExternalCredentialPrompt from './ExternalCredentialPrompt'
import ComposeFixPrompt from './ComposeFixPrompt'
import FileSetSelection, {
  fixVariantFor,
  phaseOfFile,
  EMPTY_CANDIDATES,
  EMPTY_SELECTION,
  FileSetCandidates,
  FileSetSelectionData,
  FileSetStale,
} from './FileSetSelection'

const { Text } = Typography

/**
 * GRILLING G29: TWO pools, TWO refusals, both persistent.
 *
 * `at_capacity` is the -api's (the HOST's docker pool is full); `generation_at_capacity`
 * is the gateway's own generation pool (the LLM endpoint and cost). They carry stable
 * codes precisely so the panel can show the RIGHT remedy — and the remedy text itself
 * travels in the refusal's own `detail`, so it is never invented here.
 *
 * Both must take the PERSISTENT alert below rather than a toast: a refusal means nothing
 * was accepted, and with no timeout on long builds a toast would scroll away before the
 * operator could act on it (§4d).
 */
const REFUSAL_CODES = ['at_capacity', 'generation_at_capacity']
const refusalOf = (err: any): { code: string; detail: string } | null => {
  if (err?.response?.status !== 503) return null
  const d = err?.response?.data?.detail
  const code = (d && typeof d === 'object' && d.error) || err?.response?.data?.error
  if (typeof code !== 'string' || !REFUSAL_CODES.includes(code)) return null
  const detail = typeof d === 'string' ? d
    : (typeof d?.detail === 'string' ? d.detail : '')
  return { code, detail }
}

/**
 * The platform's compose-naming convention — a compose is a file named `docker-compose*` or
 * `compose*` with a YAML extension. This mirrors the -api's `_is_compose_name` /
 * `_is_compose_candidate` (`lib/file_sets.py`); the two MUST agree.
 *
 * Matching on the extension alone is not harmless: it pulls in unrelated YAML shipped by the
 * repo (a linter config, `ellipsis.yaml`) and the per-user rendered compose
 * (`docker-compose.user-*.yml`). The preview then merges files that are not composes, `docker
 * compose config` fails, the preview 502s — and because the deploy flow awaits it, the deploy
 * never dispatches at all (measured: graphiti, whose repo ships `ellipsis.yaml`).
 */
function isComposeName(filename: string): boolean {
  const base = filename.split('/').pop() || filename
  const name = base.endsWith('.j2') ? base.slice(0, -3) : base
  if (!(name.endsWith('.yml') || name.endsWith('.yaml'))) return false
  if (!(name.startsWith('docker-compose') || name.startsWith('compose'))) return false
  if (/^docker-compose\.user-/.test(name)) return false   // per-user rendered compose
  return true
}

// The session/entry label this panel was opened ON, or null when the panel is
// a free-standing deploy (no pin). Both call sites below — the open-time pin
// and the submit-time re-assert — MUST agree on this, so the truth lives in one
// predicate rather than in two copies that can drift.
//
// A pinned panel means "this (user, service, label) already exists and must be
// reused, not drifted to a fresh label and orphaned" (failed-instance-state
// todo §5.1/§5.2; a `Retry` from a failed card and a `check` on an LLM-fix
// pause both arrive this way). `String()` because UsersPage passes `svc.label`
// raw at one site, so a number can reach us in practice.
function pinnedSessionLabel(preselectedLabel?: string | number | null): string | null {
  return preselectedLabel !== undefined && preselectedLabel !== null && preselectedLabel !== ''
    ? String(preselectedLabel)
    : null
}

// The session's OWN proxy setting this panel was opened ON, or null for a
// free-standing deploy. `check` on a paused LLM-fix session RE-ENTERS the path
// the original deploy created, and G20 rules that a resumed path replays the
// options it was created with — so the "Use global proxy" tick must come back
// from the session, exactly as the label does. The open-time seed and the
// submit-time re-assert both read THIS predicate, so the two cannot drift.
function seedUseGlobalProxy(preselectedUseGlobalProxy?: boolean | null): boolean | null {
  return preselectedUseGlobalProxy === undefined || preselectedUseGlobalProxy === null
    ? null
    : !!preselectedUseGlobalProxy
}

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
  // Retry from a failed card prefills the user too, so the same
  // (user, service, label) key is reused rather than drifting to a new one
  // (failed-instance-state-todo.md §5.1/§5.2).
  preselectedUser?: string
  // Retry pins the FAILED entry's label so the retry replaces it instead of
  // drifting to a new label (failed-instance-state-todo.md §5.1/§5.2).
  preselectedLabel?: string
  // A REFUSED deployment's stored options, applied verbatim so the panel shows
  // exactly what the -api saw last time. The pickers are seeded from the paths
  // that were SENT (not from `selection`), so files the generation path produced
  // appear selected — the same options, from the -api's perspective.
  initialOptions?: Record<string, any> | null
  // G29: a GATEWAY-side refusal (`generation_at_capacity`) was refused *before*
  // anything was produced, so its Retry must run the pipeline again — the pool
  // is the only thing that was in the way. `initialOptions` still SEEDS the
  // panel from the record; it just must not be read as "the work is already
  // done". Without this the retry would deploy paths that were never generated.
  regenerate?: boolean
  // `check` on a `paused` pre-deploy session RE-ENTERS the paused path
  // (GRILLING G15/G24): the panel opens attached to the parked generation job,
  // which is what puts its pause popup on screen. Without this the button could
  // only open a blank panel — the duplicate of `Retry` it used to be.
  resumeJobId?: number | null
  // `check` on an LLM-fix pause (GRILLING G26 row 5): the panel opens with the
  // ANALYSIS's fixing guide as the prompt, and the restart runs in fixing mode
  // so the generation emits `.fix` files rather than `.base` (G25).
  fixingGuide?: { guide: string; slot: string } | null
  // G20, applied to the `check` restart: the paused session's own proxy setting,
  // so the panel opens with the tick the original deploy was created with and
  // the restart replays it instead of registering `build_args: {}`.
  preselectedUseGlobalProxy?: boolean | null
}

export default function DeployForm({ open, onClose, onDeployed, preselectedService, preselectedUser, preselectedLabel, initialOptions, regenerate, resumeJobId, fixingGuide, preselectedUseGlobalProxy }: DeployFormProps) {
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(false)
  const [proxyEnabled, setProxyEnabled] = useState(false)
  const [httpsEnabled, setHttpsEnabled] = useState(false)
  // At-capacity refusal (doc §4d) — persistent, not a toast. Carries WHICH pool
  // refused, because the two remedies differ (G29).
  const [atCapacity, setAtCapacity] = useState<{ code: string; detail: string } | null>(null)
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
  // Values typed into the external-credential pause popup, keyed by variable
  // name. Never persisted in component state beyond the submit.
  // (external-credential values now live inside ExternalCredentialPrompt)
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
  // §5: the env REFERENCE picker — the generation CONTEXT for the `.raw`
  // (base/example env files), used only while env is `generate new`. The
  // "Interpolation env files" picker above is strictly the HANDOFF now: it is
  // what gets passed to the -api when env is selected, and is no longer used as
  // generation context. Optional; defaults to nothing.
  const [envRefs, setEnvRefs] = useState<string[]>([])
  // Deploy-time per-user env generation (GAP-19): fresh SECRET_KEY per instance.
  const [perUserEnv, setPerUserEnv] = useState<string | null>(null)
  // Fixing mode (GRILLING G25) is derived from the `fixingGuide` PROP, not from
  // state: the guide is stable for the whole open session, whereas a state
  // boolean set at init would be invisible to the async loaders that run in the
  // same tick (their closures capture the pre-update value).
  // Editor modal for reviewing generated files (clickable → built-in editor)
  const [editorModalOpen, setEditorModalOpen] = useState(false)
  const [editorFileName, setEditorFileName] = useState('')
  const [editorContent, setEditorContent] = useState('')
  const [editorSaving, setEditorSaving] = useState(false)
  // §5b: when the editor was opened from a PICKER, saving forks into the `.fix`
  // variant instead of writing back over the selected file — that is what keeps
  // the rotation rule honest (a repo-original selection is never rewritten by
  // an edit made in the panel). After saving the picker switches to the fork.
  const [editorFork, setEditorFork] = useState(false)

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
        // Convention, not extension: `ellipsis.yaml` and the per-user compose are not composes,
        // and merging them makes the preview fail (see isComposeName).
        const plain = candidates.filter((f: string) => isComposeName(f) && !f.endsWith('.j2'))
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
    // Retry from a failed card PINS the failed entry's own label: the entry
    // keeps its (user, service, label) key, so a retry must replace it rather
    // than drift to a fresh label and orphan the failed one
    // (failed-instance-state-todo.md §5.1/§5.2). Allocation would otherwise
    // return max+1, because the failed entry still counts.
    if (preselectedLabel !== undefined && preselectedLabel !== '') {
      setNextLabel(preselectedLabel)
      form.setFieldsValue({ label: preselectedLabel })
      setComputingLabel(false)
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
        // Seed the session's proxy tick SYNCHRONOUSLY, before the first await.
        // The label pin used to sit at the END of this async `init()`, behind
        // `await loadSources()`, which left a ~600 ms window where the field was
        // wrong; a checkbox read at Confirm inside that window would submit
        // `use_global_proxy: false` for a proxied session. Seeding first removes
        // the window entirely for the normal open; the submit-time re-assert
        // below covers the case where this effect never re-runs.
        proxyTouchedRef.current = false
        const seedProxy = seedUseGlobalProxy(preselectedUseGlobalProxy)
        if (seedProxy !== null) form.setFieldsValue({ use_global_proxy: seedProxy })
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
        setAtCapacity(null)
        // `check` on an LLM-fix pause (G26 row 5): the analysis's guide IS the
        // prompt, and the restart runs in fixing mode so the generation emits
        // `.fix` files (G25). The slot the analysis named goes to `generate new`
        // — "only set the problematic one" — so the rest of the path is reused
        // as it was. A later reset on service change does not clear this: the
        // guide belongs to the pause, not to the picker.
        if (fixingGuide) setPrompt(fixingGuide.guide || '')
        // Re-attach to the parked generation job, which is what surfaces its
        // pause popup (compose conflict / external credential) in this panel.
        if (resumeJobId != null) genJob.resumeJob(resumeJobId)
        try {
          const { data: cfg } = await client.get('/llm/config')
          setLlmActive(!!(cfg && (cfg.is_active || cfg.byok_configured)))
        } catch { setLlmActive(false) }
        // Preselect service if provided
        if (initialOptions) {
          const f: Record<string, any> = {}
          for (const k of ['user_name', 'label', 'domain', 'passwd', 'https', 'use_global_proxy']) {
            if (initialOptions[k] !== undefined && initialOptions[k] !== null) f[k] = initialOptions[k]
          }
          const svc = initialOptions.service_name
          if (svc) f.service_name = initialOptions.project_root && initialOptions.project_root !== svc
            ? `${svc}@@${String(initialOptions.project_root).slice(String(svc).length + 1)}`
            : svc
          form.setFieldsValue(f)
        }
        if (preselectedUser) {
          form.setFieldsValue({ user_name: preselectedUser })
        }
        if (preselectedService) {
          form.setFieldsValue({ service_name: preselectedService })
          checkMissingFiles(preselectedService)
          fetchExpectedVolumes(preselectedService, sourcesData)
        }
        // PIN the label on open, not only inside `computeNextLabel`.
        //
        // That helper honours `preselectedLabel`, but it only RUNS from the two
        // Select onChange handlers — so a panel opened with a preselected
        // service never called it, the field kept the form's initial `0`, and
        // the pin was unreachable exactly when it was needed. A `Retry` from a
        // failed card (failed-instance-state-todo.md §5.1/§5.2) and a `check`
        // on an LLM-fix pause both arrive through this path, and both must
        // reuse the entry's own (user, service, label) rather than drift to a
        // fresh label and orphan it.
        const pinnedOnOpen = pinnedSessionLabel(preselectedLabel)
        if (pinnedOnOpen !== null) {
          setNextLabel(pinnedOnOpen)
          form.setFieldsValue({ label: pinnedOnOpen })
        }
      }
      init()
    }
  }, [open, preselectedService, preselectedUser])

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
      if (initialOptions) {
        // The SENT paths are the truth: a generated `.base` / `.raw` that went
        // to the -api must show as SELECTED, not as `generate new`. This is what
        // makes a refused Retry show "the exact same options from the -api's
        // perspective". (First written with a bare replace() whose anchor did
        // not match, so it silently never applied — caught by trace T2.)
        const sentCompose = initialOptions.compose_file_paths
          || (initialOptions.compose_file_path ? [initialOptions.compose_file_path] : null)
        const sentEnv = initialOptions.env_files
          || (initialOptions.env_file_path ? [initialOptions.env_file_path] : null)
        setSelection(prev => ({
          compose: Array.isArray(sentCompose) && sentCompose.length ? sentCompose : prev.compose,
          nginx: initialOptions.nginx_conf_file_path || prev.nginx,
          env: Array.isArray(sentEnv) && sentEnv.length ? sentEnv : prev.env,
          profiles: initialOptions.profiles || prev.profiles,
        }))
      }
      // G26 row 5 — `check` on an LLM-fix pause: the analysis named the faulty
      // slot, so THAT slot goes to `generate new` and the rest of the path is
      // reused as it stands. "Only the problematic one" is what keeps a fix from
      // gratuitously regenerating files that were never at fault.
      if (fixingGuide) {
        const slot = fixingGuide.slot || 'compose'
        setSelection(prev => ({
          ...prev,
          compose: slot === 'compose' ? [] : prev.compose,
          nginx: slot === 'nginx' ? null : prev.nginx,
          env: slot === 'env' ? [] : prev.env,
        }))
      }
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

  // §6: the alert and the Generate button speak ONLY about this panel's
  // selections — never about what the recipe happens to contain. `generate new`
  // is the absence of a selection, so "ready to deploy" means all three slots
  // name a specific file.
  const eff = effectiveSelection()
  const slotsSpecified = eff.compose.length > 0 && !!eff.nginx && eff.env.length > 0

  // Async generation job (ordered phases compose → nginx → env, one job).
  const genJob = useGenerationJob()
  const [genValidations, setGenValidations] = useState<Record<string, any>>({})
  // Deploy progress popup (design §8.3) — shows generation steps + deploy step.
  const [genPopupOpen, setGenPopupOpen] = useState(false)
  // True while handleDeploy's one-shot auto-gen runs — the popup FREEZES on the
  // completed frame (no reset, no "review, then deploy" toast) until /users/deploy
  // returns; the manual Generate button keeps the review-gate behavior (feature 4).
  const deployAutoRunRef = useRef(false)
  // Did the operator touch the "Use global proxy" checkbox in THIS panel? The
  // session's tick is a DEFAULT a free-standing panel does not have — it is
  // re-asserted at submit (like the pinned label) only while the field is still
  // untouched, so an explicit toggle is never overridden.
  const proxyTouchedRef = useRef(false)

  // The proxy setting THIS submission must carry, for EVERY entry point.
  //
  // G20: a resumed path replays the options it was created with, so the
  // session's OWN value (`preselectedUseGlobalProxy`, set by `check` on a paused
  // session) is a DEFAULT that wins until the operator touches the box; an
  // explicit toggle always wins. Both the deploy path (`handleDeploy`) and the
  // generate path (`generateMissingFiles`) go through THIS predicate, so the two
  // entry points cannot drift.
  //
  // The generate path used to omit `use_global_proxy` from its `startJob`
  // payload entirely. The gateway then recorded the pre-deploy session that job
  // creates with `use_global_proxy: false`, so a later `check` on that parked
  // session replayed `false` while the panel's box had been ticked — found live
  // on T12 (`finalgrill|probe-conflict||1`). The deploy path had already been
  // fixed for exactly this (it populates `snap.use_global_proxy`); the generate
  // entry point is the same class of request and was missed.
  const effectiveUseGlobalProxy = (): boolean => {
    const seeded = seedUseGlobalProxy(preselectedUseGlobalProxy)
    if (seeded !== null && !proxyTouchedRef.current) return seeded
    return !!form.getFieldValue('use_global_proxy')
  }

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
        // G25 — the NAMING follows the TRIGGER: on a guided restart the
        // generation emits `.fix` files, not `.base`.
        ...(fixingGuide ? { fix: true } : {}),
        selection: effectiveSelection(),
        // G20 — the session this job opens must record the proxy the panel is
        // showing, or a `check` restart replays `false` (see
        // `effectiveUseGlobalProxy`).
        use_global_proxy: effectiveUseGlobalProxy(),
        user_name: form.getFieldValue('user_name') || '',
        label: form.getFieldValue('label') || '0',
        deploy_metadata: {
          service_name: baseName,
          user_name: form.getFieldValue('user_name') || '',
          label: form.getFieldValue('label') || '0',
          domain: form.getFieldValue('domain') || 'localhost',
          https: !!form.getFieldValue('https'),
          // The URL and the credential belong here because they belong to the
          // REFUSAL record: if the gateway's pool refuses this generation, that
          // record is what the card's Retry replays, and a retry missing either
          // is a different deployment (user ruling 2026-09-26).
          passwd: form.getFieldValue('passwd') || '',
        },
      })
    } catch (err: any) {
      // §4d: a capacity refusal is NOT a failed generation — nothing was
      // accepted, and the remedy is to cancel what is hanging. It must raise
      // the SAME persistent alert as the deploy path (a toast would scroll
      // away), and a structured `detail` must not be handed to message.error.
      const d = err.response?.data?.detail
      const refusal = refusalOf(err)
      if (refusal) {
        setAtCapacity(refusal)
      } else {
        // Structured 4xx details are OBJECTS — `{error, detail}` — and reading
        // `detail` as a string collapsed every one of them into the generic
        // "Failed to start generation job". The common case is the gateway's
        // 409 `combination_reserved`, whose whole point is to say that a
        // pre-deploy session already holds this (user, service, label) and must
        // be checked, cancelled or deleted first. Printing the generic message
        // hid exactly the instruction the operator needed.
        message.error(
          typeof d === 'string' ? d
            : (typeof d?.detail === 'string' ? d.detail
              : (typeof err.response?.data?.error === 'string' ? err.response.data.error
                : (typeof err.response?.data?.detail === 'string' ? err.response.data.detail
                  : 'Failed to start generation job')))
        )
      }
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
      // G25 — a guided restart generates `.fix` files, not `.base`.
      ...(fixingGuide ? { fix: true } : {}),
      // §5: the reference is CONTEXT for the env phase, not a handoff.
      env_reference: envRefs,
      selection: effectiveSelection(),
      // G20 — same as the generate entry point: the session this job opens must
      // record the proxy. Read from the SNAPSHOT, never `form.getFieldValue`:
      // `handleDeploy` already closed the panel (destroyOnClose), so the live
      // form is gone here. `snap.use_global_proxy` was set by the shared
      // `effectiveUseGlobalProxy` predicate at submit.
      use_global_proxy: !!snap.use_global_proxy,
      user_name: snap.user_name || '',
      label: snap.label || '0',
      deploy_metadata: {
        service_name: baseName,
        user_name: snap.user_name || '',
        label: snap.label || '0',
        domain: snap.domain || 'localhost',
        https: !!snap.https,
        // Same as the entry-point payload above: the refusal record the card's
        // Retry replays must carry the URL and the credential.
        passwd: snap.passwd || '',
      },
    })
    if (!jobId) throw new Error('Failed to start generation job')
    for (let i = 0; i < 900; i++) {
      await new Promise(r => setTimeout(r, 1000))
      // G27: read the CURRENT id on every pass, never the one captured here.
      // Answering a pause creates a NEW job, and a loop pinned to the id it
      // started with polls a row that is terminal forever — while the popup,
      // driven by the hook, already shows the resumed leg complete. The deploy
      // was then never issued and the operator stared at "deploying the
      // instance…" with nothing happening. Found live in T13.
      const pollId = genJob.jobIdRef.current ?? jobId
      const { data } = await client.get(`/llm/jobs/${pollId}`)
      if (data.status === 'completed') {
        const result = data.result || {}
        const name = result.env_name || null
        const files = result.files || {}
        if (name) setPerUserEnv(name)
        if (Object.keys(files).length > 0) setGeneratedFiles(files)
        // Return the fresh results synchronously — React state updates are not
        // visible inside this still-running async flow, and the deploy payload
        // must reference the just-generated env.raw name + files NOW.
        // G21 rule 3: hand back the selection AS THE PATH LEFT IT. A path that
        // resumed after a pause reports only the file that leg produced (e.g.
        // the env), so the caller's `files[...]` checks cannot see a compose
        // regenerated EARLIER in the same path — which is exactly the conflict
        // fix. Returning the selection is what carries it into the payload.
        return { envName: name, files, selection: result.selection || null }
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
    // §6: the pickers FLIP to what was generated (not "remembered as last
    // generated"), which is what makes the ready-alert appear and the Generate
    // button disappear. Resetting a picker to `generate new` brings them back.
    // G21 rule 3: adopt the selection AS THE PATH LEFT IT. The gateway reports
    // it because a generated artifact REPLACES its slot — and on a resumed
    // conflict fix the replacement is the `.fix` variant, which the hardcoded
    // `.base` checks this used to do could never see. The deploy payload is
    // built from THIS selection, so without the swap the deploy would still send
    // the compose the pipeline just replaced.
    const resultSel: any = (st.result && st.result.selection) || null
    if (resultSel) {
      setSelection(prev => {
        const next: any = { ...prev }
        // Adopt only slots the path actually reports: absent or EMPTY means
        // "unchanged", not "cleared". A fix job's request carries no selection
        // of its own, so reading `[]` as "clear" would wipe the operator's picks.
        if (Array.isArray(resultSel.compose) && resultSel.compose.length) next.compose = resultSel.compose
        if (typeof resultSel.nginx === 'string' && resultSel.nginx) next.nginx = resultSel.nginx
        if (Array.isArray(resultSel.env) && resultSel.env.length) next.env = resultSel.env
        return next
      })
    } else {
      // No reported selection (an older row, or a job that carried none): fall
      // back to what THIS run produced. Matched by the platform's own candidate
      // predicates, never by exact name — a generated artifact carries the job's
      // collision token (`docker-compose.base.337.yml`, GRILLING G33), so the
      // exact-name lookups this replaces stopped firing the moment a token
      // appeared, and the deploy kept the un-generated compose.
      const producedCompose = Object.keys(files).find(isComposeName)
      if (producedCompose) setSelection(prev => ({ ...prev, compose: [producedCompose] }))
      // nginx: `*.conf` and NOT a template — the -api's own predicate, which the
      // gateway's candidate scan mirrors (both engines MUST agree).
      const producedNginx = Object.keys(files).find(n => n.endsWith('.conf') && !n.endsWith('.j2'))
      if (producedNginx) setSelection(prev => ({ ...prev, nginx: producedNginx }))
    }
    if (!isDeployAuto) genJob.reset()
  }, [genJob.jobState])

  // Save an edited generated file back to state + disk
  // §5b: open a SELECTED file for view/edit. Fetch it from the recipe through
  // the same project-files endpoint the generated-file editor uses.
  const openSelectedFile = async (name: string) => {
    const svcVal = form.getFieldValue('service_name') || ''
    const { baseName, recipePath } = parseServiceValue(svcVal)
    const path = recipePath ? `${recipePath}/${name}` : name
    try {
      const { data } = await client.get(`/projects/${baseName}/files/${path}`)
      setEditorFileName(name)
      setEditorContent(typeof data?.content === 'string' ? data.content : '')
      setEditorFork(true)
      setEditorModalOpen(true)
    } catch (err: any) {
      message.error(err.response?.data?.detail || `Could not open ${name}`)
    }
  }

  const saveEditorFile = async () => {
    if (!editorFileName) return
    setEditorSaving(true)
    try {
      const targetName = editorFork ? fixVariantFor(editorFileName) : editorFileName
      // Merge the edited content into the generated-files state
      setGeneratedFiles(prev => ({ ...prev, [targetName]: editorContent }))
      // Persist to the service project on disk
      const svcVal = form.getFieldValue('service_name') || ''
      const { baseName, recipePath } = parseServiceValue(svcVal)
      await client.post('/projects/save-generated', {
        service_name: baseName,
        recipe_path: recipePath,
        files: { [targetName]: editorContent },
      })
      if (editorFork) {
        // The picker switches to the fork automatically, so the next deploy
        // uses the edited file without re-selecting anything.
        setSelection(prev => ({
          ...prev,
          compose: prev.compose.map(p => (p === editorFileName ? targetName : p)),
          nginx: prev.nginx === editorFileName ? targetName : prev.nginx,
          env: prev.env.map(p => (p === editorFileName ? targetName : p)),
        }))
        message.success(`Saved as ${targetName} — the selection now uses it`)
      } else {
        message.success(`${editorFileName} saved`)
      }
      setEditorModalOpen(false)
      setEditorFork(false)
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
    // ---- The label that is actually SENT is re-asserted HERE -----------------
    //
    // `values.label` is NOT authoritative whenever this panel was opened on a
    // pinned entry. The open-time pin lives at the END of the async `init()`,
    // behind `await loadSources()`, so for the first ~600 ms after the panel
    // opens the (disabled) Label field still shows the Form's `initialValues`
    // `label: '0'` — measured live: `0` at t=587 ms, correct and stable only
    // from t=692 ms. The operator can neither see nor correct it (the field is
    // disabled), and a Confirm inside that window submitted `request.label='0'`
    // for a label-2 session, which generated `.env.raw.alice.0` instead of
    // `.env.raw.alice.2`.
    //
    // Disabling Confirm until the pin applies would only shift the race (it
    // still needs a "pin applied" signal, and the button has other async gates
    // already). Reading the pin from the SAME source the open-time pin reads —
    // the session/entry's own label, which is what `preselectedLabel` carries —
    // at the last moment before the request is built makes an unpinned
    // submission structurally impossible, however fast Confirm is clicked.
    // `snap.label` feeds both the generation job and the deploy payload below,
    // so re-asserting it once covers every consumer.
    const pinnedAtSubmit = pinnedSessionLabel(preselectedLabel)
    if (pinnedAtSubmit !== null) snap.label = pinnedAtSubmit
    // ---- G20, the same shape for the proxy --------------------------------
    //
    // `check` on a paused LLM-fix session re-enters the path the original
    // deploy created, so the restart must replay its proxy setting. Seeded from
    // the session (not from `values`, which is only authoritative once the
    // async open-time seed has run) UNLESS the operator toggled the box in this
    // panel — an explicit choice always wins.
    //
    // The predicate is SHARED with the generate entry point
    // (`effectiveUseGlobalProxy`), so the two cannot drift. `snap` is the
    // carrier: every request built below (the generation job AND the deploy
    // payload) reads it, and the panel is gone by then (destroyOnClose), so
    // `form.getFieldValue` must not be read again after this point.
    snap.use_global_proxy = effectiveUseGlobalProxy()
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
      // A REFUSED retry must NOT run the generation pipeline (GRILLING G20).
      // Its options are already complete — they are the paths the -api was SENT.
      // Running generation here is not merely wasteful: the refused deployment's
      // OWN pre-deploy session reserves the combination (§8b), so the generation
      // job is refused with 409 and the deploy never dispatches. Found live by
      // trace T3 — the feature was self-blocking.
      // §2/§4: a SELECTED env IS the handoff — "when the operator *did* select an
      // env, that selection is the handoff instead" of the generated `.raw`. The
      // condition used to force the per-user env generation even then, which
      // contradicts the design AND made every Deploy pay a ~30s generation window
      // before the deploy POST — measured across Rounds 24-25 of trace T5, where
      // it also made a live capacity refusal unreachable.
      const hasSelectedEnv = effectiveSelection().env.length > 0
      // G13/G20: `generate new` means NOT SELECTED, and a deploy must send the
      // -api ALL THREE paths — a refused deployment carries all three, because
      // any slot left at `generate new` had a file generated and sent for it.
      // So an unselected slot is a reason to generate, independently of what the
      // recipe happens to contain on disk. Without `!slotsSpecified` a deploy
      // whose nginx slot sat at `generate new` while an `nginx.conf` existed
      // skipped generation entirely, and the payload carried NO nginx path at
      // all — found live on `bob|probe-interp||3`.
      // …except a GATEWAY refusal's retry (`regenerate`), whose record carries
      // options only as a SEED: nothing was generated, so the pipeline must run.
      const needsGeneration = (!initialOptions || regenerate) && (
        !slotsSpecified ||
        missingFiles.length > 0 ||
        (needsEnv && !perUserEnv && !hasSelectedEnv)
      )
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
        // §6/§7: a `generate new` slot FLIPS to the file the generation just
        // produced, so the selection that rides this deploy payload (and is
        // persisted as the next default) names the generated file rather than
        // leaving the slot unset. Without this the next deployment of the same
        // service would regenerate instead of pre-selecting what was deployed.
        // G21 rule 3 — adopt the selection the path reported, FIRST. The
        // generated artifacts REPLACE their slots, and on a resumed path the
        // replacement happened in an earlier leg, so it is only visible here.
        const resSel = (res && res.selection) || null
        if (resSel) {
          if (Array.isArray(resSel.compose) && resSel.compose.length) sel.compose = resSel.compose
          if (typeof resSel.nginx === 'string' && resSel.nginx) sel.nginx = resSel.nginx
          if (Array.isArray(resSel.env) && resSel.env.length) sel.env = resSel.env
        } else {
          // No reported selection — name what THIS run produced instead, by
          // PATTERN (GRILLING G33: a generated artifact carries the job's
          // collision token, so the exact-name lookups this replaces stopped
          // matching and the payload would keep the file the run just replaced).
          const producedCompose = Object.keys(genNow).find(isComposeName)
          if (producedCompose) sel.compose = [producedCompose]
          const producedNginx = Object.keys(genNow).find(n => n.endsWith('.conf') && !n.endsWith('.j2'))
          if (producedNginx) sel.nginx = producedNginx
        }
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

      // G20 — the deploy hands the -api ALL THREE paths: any slot left at
      // `generate new` had a file generated and sent for it, so a refused
      // deployment always carries all three. The gateway swaps a generated
      // nginx into the selection, so this is normally already named; when it is
      // not, take the nginx file this run produced rather than sending a
      // deployment with no nginx at all — that instance would deploy unroutable
      // and the edge would 502 its /go/ relay.
      if (!sel.nginx) {
        const produced = Object.keys(gen).find(n => n.endsWith('.conf') || n.endsWith('.conf.j2'))
        if (produced) sel.nginx = produced
      }

      // What was SELECTED is what is sent — no fallback chain
      // (deploy-panel-selection-and-generation-todo.md §2). The panel cannot
      // know which convention-matching file is the right base, and guessing
      // picked a test overlay (graphiti's docker-compose.test.yml) or let a
      // stale `.j2` shadow a newer plain file. A slot left at `generate new`
      // contributes NOTHING here — the generation supplies it.
      if (sel.compose.length > 0) {
        payload.compose_file_paths = sel.compose
      }
      // nginx is no longer "optional": it is governed by the same rule, and its
      // absence is as fatal as a missing compose (the instance would deploy
      // unroutable and the edge would 502 the /go/ relay).
      if (sel.nginx) {
        payload.nginx_conf_file_path = sel.nginx
      }
      // A selected env is passed even when EMPTY: the only guard is presence,
      // never size or content. An empty `--env-file` is exactly what the
      // always-pass invariant wants, since it keeps compose's auto-read of the
      // recipe `.env` overridden.
      if (sel.env.length > 0) {
        payload.env_files = sel.env
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
      const d = err.response?.data?.detail
      // A capacity refusal is NOT a failed deploy — nothing was accepted, and
      // the remedy is to cancel whatever is hanging. It gets a PERSISTENT
      // alert instead of a toast (doc §4d): with no timeout on long builds
      // (GRILLING G11), this is the only way saturation is discoverable. Both
      // pools' codes land here (G29) — the -api's and the gateway's own.
      const refusal = refusalOf(err)
      if (refusal) {
        setAtCapacity(refusal)
      } else {
        message.error(typeof d === 'string' ? d : 'Deploy failed')
      }
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

  // External-credential pause (task doc §2): the shared prompt is rendered in BOTH surfaces that
  // can be on screen while a job runs — this panel's inline progress area, and the separate
  // "Deploying…" popup, which is what the auto-run deploy actually shows.
  // Persistent at-capacity alert (doc §4d). Rendered on BOTH surfaces for the
  // same reason the credential prompt is: the auto-run deploy CLOSES this panel
  // and shows the "Deploying…" popup, so an alert living only in the panel body
  // is invisible exactly when a generation is refused — found live, where the
  // refusal produced no visible alert at all.
  const renderAtCapacity = () => (
    atCapacity ? (
      <Alert
        type="error"
        showIcon
        closable
        onClose={()=>setAtCapacity(null)}
        style={{marginBottom:16}}
        message={atCapacity.code === 'generation_at_capacity'
          ? "The generation pool is full — nothing was queued"
          : "Platform is at capacity — nothing was queued"}
        description={<>
          {atCapacity.detail || (atCapacity.code === 'generation_at_capacity'
            ? 'Wait for a running generation to finish, or cancel one.'
            : 'Cancel a task that has been hanging, then retry.')}
          <div style={{marginTop:6}}>
            <Button size="small" onClick={()=>{ onClose(); window.location.href = '/tasks' }}>
              {atCapacity.code === 'generation_at_capacity'
                ? 'Go to Tasks and cancel a running generation'
                : 'Go to Tasks and cancel a hanging task'}
            </Button>
          </div>
        </>}
      />
    ) : null
  )

  const renderExternalPrompt = () => {
    if (!(isPaused(genJob.jobState) && (genJob.jobState.pending_input || {}))) return null
    // G27: the leg is TERMINAL. Its secondary button therefore DISMISSES this
    // view — it must not offer to cancel a job that has already stopped, and it
    // must not look like it did: the paused record stays on the Tasks page,
    // where the pause can be answered again.
    const _terminal = genJob.jobState.status === 'paused'
    const _onCancel = _terminal ? () => genJob.reset() : genJob.cancelJob
    const _label = _terminal ? 'Close' : 'Cancel'
    // The pause has two kinds (§3b): awaiting the operator's credential
    // VALUES, or awaiting an editable FIX GUIDANCE for a compose conflict.
    return (genJob.jobState.pending_input || {}).kind === 'compose_fix' ? (
      <ComposeFixPrompt
        jobState={genJob.jobState}
        onSubmit={genJob.submitInput}
        onCancel={_onCancel}
        cancelLabel={_label}
      />
    ) : (
      <ExternalCredentialPrompt
        jobState={genJob.jobState}
        onSubmit={genJob.submitInput}
        onCancel={_onCancel}
        cancelLabel={_label}
      />
    )
  }

  return (
    <>
    {/* §4d's persistent at-capacity alert lives at the ROOT, not in a modal.
        Rendered inside the panel or the "Deploying…" popup it was invisible
        whenever the OTHER surface was the one on screen — measured: /api/llm/jobs
        returned 503 while no alert appeared. Root-level cannot be hidden by
        whichever modal happens to be open. */}
    {renderAtCapacity()}
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
            onOpenFile={(name) => openSelectedFile(name)}
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

          {/* §5: shown ONLY when env is `generate new` (an empty env slot). It
              supplies the context the model reads to produce the per-user
              `.raw`; selecting a handoff env above makes it irrelevant. */}
          {eff.env.length === 0 && (fileSetCandidates.env || []).length > 0 && (
            <div style={{ marginTop: 8 }}>
              <Text strong>Env reference (generation context)</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {' '}— base/example env files the model reads to produce the per-user env.
                Not passed to the deployment.
              </Text>
              <Select
                mode="multiple"
                allowClear
                style={{ width: '100%', marginTop: 4 }}
                placeholder="None — the model works from the compose alone"
                value={envRefs}
                onChange={(v) => setEnvRefs(v as string[])}
                options={(fileSetCandidates.env || []).map(c => ({ value: c, label: c }))}
              />
            </div>
          )}

          {/* ---- Missing Files / LLM Generation ---- */}
          {checkingMissing ? (
            <div style={{padding:'8px 0'}}><Spin size="small" /> <span style={{fontSize:12,color:'#999'}}>Checking deployment readiness...</span></div>
          ) : checkError ? (
            <Alert type="error" message={`Deployment readiness check failed: ${checkError}`} style={{marginBottom:12}} />
          ) : !slotsSpecified ? (
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
        {renderExternalPrompt()}
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
                          // By NAME SHAPE, not by a table of exact names — the
                          // table this replaces listed `docker-compose.yml` and
                          // `nginx.conf`, so it matched nothing a generation
                          // actually produced and every row showed a green ✓.
                          const phase = phaseOfFile(fn)
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
            <Alert type="success" message="All necessary files are specified, ready for deployment" style={{marginBottom:12}} />
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
            <Checkbox disabled={!proxyEnabled} onChange={() => { proxyTouchedRef.current = true }}>
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
              // `computingLabel` gates the submit too: the next-label call resolves through the
              // -api's status computation and was measured taking ~3–6s, during which the
              // (read-only) Label field still shows the PREVIOUS value. Submitting in that window
              // sends the stale label — e.g. 0 for a pair that already has 0 — and the register
              // then fails with "already registered", which is the collision the label work exists
              // to prevent. The field already shows a spinner while computing; the button must
              // agree with it.
              // Both "no compose" clauses are waived on a GUIDED RESTART
              // (`fixingGuide`): there, a compose left at `generate new` is the
              // OPERATOR'S INSTRUCTION to regenerate the very file the analysis
              // found fault with. The clauses exist to catch a forgotten
              // selection — applying them here blocks the one action the whole
              // analysis step exists to enable. Found live in T13: `check`
              // opened the panel with the guide pre-filled and Deploy sat
              // disabled with no way forward.
              disabled={!selectedUserName || !selectedServiceName || !!checkError || fileSetLoading
                || computingLabel
                || (!fixingGuide && !fileSetLoading && (fileSetCandidates.compose || []).length > 0 && effectiveSelection().compose.length === 0)
                || (!fixingGuide && missingFiles.length > 0 && Object.keys(generatedFiles).length === 0 && effectiveSelection().compose.length === 0)}>
              Deploy
            </Button>
          </Space>
          {/* A DISABLED Deploy with no reason is a dead end — found live: a stored
              selection whose files had gone missing (converted to `.j2` by the -api's
              register-time conversion, or purged) left the button disabled with the
              only signal a `stale` tag buried inside a picker, and the panel was
              silently undeployable for two rounds of traces. Say why, next to the
              button that is refusing. */}
          {!loading && !selectedUserName && (
            <Alert type="info" showIcon style={{ marginTop: 8 }}
              message="Select a user and a service to deploy." />
          )}
          {/* GUIDED RESTART (`fixingGuide`) is WAIVED here exactly as it is on the
              Deploy button (see the disabled condition above): the analysis
              deliberately empties the faulty slot so the pipeline regenerates it,
              and Deploy stays enabled for that to work. This alert's condition did
              not carry the waiver, so a live `check` (G26 row 5) showed "No compose
              file is selected … Pick a compose above to deploy" next to an ENABLED
              Deploy — the message asserted the opposite of what the button does.
              Found live in the consolidated round (deviation 4). Suppress it in
              fixing mode; the (pre-filled) guide in the prompt is the real signal. */}
          {!loading && selectedUserName && selectedServiceName && !fileSetLoading && !computingLabel && !fixingGuide
            && (fileSetCandidates.compose || []).length > 0 && effectiveSelection().compose.length === 0 && (
            <Alert type="warning" showIcon style={{ marginTop: 8 }}
              message="No compose file is selected for this deployment"
              // Two very different causes reach this same alert, so it must not
              // assert one of them. The condition is "candidates exist but none
              // is selected", which covers BOTH a stored selection whose files
              // vanished AND a recipe that simply has no stored selection yet.
              // It used to claim the first unconditionally — telling the operator
              // to hunt for "stale entries tagged in the picker" on a brand-new
              // recipe where nothing is stale and none exist. Found live in
              // Round 34 while deploying a fresh fixture (defect 12).
              description={
                ((fileSetStale || {}).compose || []).length > 0
                  ? 'The stored selection names files that no longer exist (they may have been converted to .j2 templates or removed). Pick a compose above — the stale entries are tagged in the picker.'
                  : 'This recipe has no stored selection yet. Pick a compose above to deploy.'
              } />
          )}
        </Space>
      </Form>
    </Modal>

    {/* ---- Deploy progress popup (design §8.3) — closeable anytime ---- */}
    <Modal
      title={isPaused(genJob.jobState) ? 'Paused — waiting for you' : 'Deploying…'}
      open={genPopupOpen}
      onCancel={() => setGenPopupOpen(false)}
      footer={null}
      width={560}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={6}>
        {/* The pause is rendered here too: during the auto-run deploy this popup
            is the visible surface, and a paused leg matches none of the statuses
            below — without this the popup would sit empty (titled "Deploying…")
            while the deployment waits for the operator. G27 made that leg
            TERMINAL, so the pause no longer arrives as `awaiting_input`. */}
        {renderExternalPrompt()}
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
