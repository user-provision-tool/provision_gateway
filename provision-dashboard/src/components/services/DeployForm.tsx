import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { Modal, Form, Input, Select, Button, Switch, Space, Divider, message, Checkbox, Alert, Spin, Tag, Typography, Collapse } from 'antd'
import { PlusOutlined, MinusCircleOutlined, GlobalOutlined, RobotOutlined, DownloadOutlined, WarningOutlined, CheckCircleTwoTone } from '@ant-design/icons'
import Editor from '@monaco-editor/react'
import client from '../../api/client'
import { deriveProfiles, getComposePreview } from '../../api/services'
import { useGenerationJob, isPaused } from '../../hooks/useGenerationJob'
import PausePrompt from './PausePrompt'
import BaseFilesPopup, {
  BaseRefs,
  DEPLOY_REF_SLOTS,
  EMPTY_BASE_REFS,
  activeBaseRefs,
} from './BaseFilesPopup'
import AdditionalContextPopup, {
  AdditionalContext,
} from './AdditionalContextPopup'
import { setServiceContext, useServiceContext } from '../../stores/additionalContext'
import FileSetSelection, {
  SelectedFileRow,
  confCategory,
  userVariantFor,
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
const REFUSAL_CODES = ['at_capacity', 'generation_at_capacity',
  // G92 — the bounded queue's own refusals: `queue_full` (20 already queued)
  // and `queue_unavailable` (stale planner heartbeat). Same persistent alert.
  'queue_full', 'queue_unavailable']
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
  // GRILLING G90 / task trace T0: the card-creating contract. `onDeployStarted`
  // fires SYNCHRONOUSLY at submit (after the client guard, before the POST) so
  // the page inserts the blinking placeholder; `onDeployFailed` fires on any
  // rejection so the placeholder is removed and no waiting card is orphaned.
  onDeployStarted: (user: string, service: string, label: string) => void
  onDeployFailed: (user: string, service: string, label: string) => void
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
  resumeJobId?: string | null
  // `check` on an LLM-fix pause (GRILLING G26 row 5): the panel opens with the
  // ANALYSIS's fixing guide as the prompt, and the restart runs in fixing mode
  // so the generation emits `.fix` files rather than `.base` (G25).
  fixingGuide?: { guide: string; slot: string } | null
  // G20, applied to the `check` restart: the paused session's own proxy setting,
  // so the panel opens with the tick the original deploy was created with and
  // the restart replays it instead of registering `build_args: {}`.
  preselectedUseGlobalProxy?: boolean | null
}

export default function DeployForm({ open, onClose, onDeployed, onDeployStarted, onDeployFailed, preselectedService, preselectedUser, preselectedLabel, initialOptions, regenerate, resumeJobId, fixingGuide, preselectedUseGlobalProxy }: DeployFormProps) {
  const [form] = Form.useForm()
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)
  const [proxyEnabled, setProxyEnabled] = useState(false)
  const [httpsEnabled, setHttpsEnabled] = useState(false)
  // At-capacity refusal (doc §4d) — persistent, not a toast. Carries WHICH pool
  // refused, because the two remedies differ (G29).
  const [atCapacity, setAtCapacity] = useState<{ code: string; detail: string } | null>(null)
  const [sources, setSources] = useState<{ name: string; files: string[]; has_compose_template?: boolean; recipes?: { path: string; label: string; is_root: boolean; template_files: string[] }[] }[]>([])
  const [deployableUsers, setDeployableUsers] = useState<{id:string,username:string,label:string}[]>([])
  // G102 — the dependency picker: services grouped by OWNER ACCOUNT, and only
  // the accounts the deploying user can reach (own + active account grant +
  // public). Same-owner is listed like any other dependency.
  const [crossTargets, setCrossTargets] = useState<
    {key:string,label:string,options:{value:string,label:string}[]}[]
  >([])
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
  // R1/R3/R26–R28 — the per-slot base refs, the arbitrary context, and the
  // classification/relation selections. The force control is retired (C21):
  // the classification picker's `generate new` and the per-target relation
  // pickers are the explicit controls.
  const [baseRefs, setBaseRefs] = useState<BaseRefs>(EMPTY_BASE_REFS)
  // '' means "the picker's default" (a matched valid variant, else generate
  // new). Resets with the panel, like the other per-deploy controls.
  const [classificationChoice, setClassificationChoice] = useState('')
  const [relationChoices, setRelationChoices] = useState<Record<string, string>>({})
  const [classificationState, setClassificationState] = useState<any>(null)
  // Bumped after a variant edit/save so the coverage/1:1 checks re-run
  // immediately (R26/R28: "the check re-runs on save").
  const [classificationRefresh, setClassificationRefresh] = useState(0)
  const [basePopupOpen, setBasePopupOpen] = useState(false)
  const [contextPopupOpen, setContextPopupOpen] = useState(false)
  // Deploy-time per-user env generation (GAP-19): fresh SECRET_KEY per instance.
  const [perUserEnv, setPerUserEnv] = useState<string | null>(null)
  // S13 — the LLM env guard. Enabled + checked by default when the LLM is
  // configured; it applies ONLY to a selected env file (not `generate new`,
  // not `None`) and never replaces the mechanical validators.
  const [llmValidateEnv, setLlmValidateEnv] = useState(false)
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

  // The pre-deploy session's own identity, captured while polling it. The
  // add-then-remove handoff sends these so the -api adopts the SAME service_id.
  const sessionServiceIdRef = useRef<string | null>(null)
  const sessionProcedureIdRef = useRef<string | null>(null)

  // GAP-003: Auto-computed next label state
  const [nextLabel, setNextLabel] = useState<string>('0')
  const [computingLabel, setComputingLabel] = useState(false)

  // Auto-detected volume keys from compose template
  const [expectedVolumeKeys, setExpectedVolumeKeys] = useState<string[]>([])
  // G60 (D8) — the golden per-key paths, computed by the -api. The dashboard
  // never assembles `{root}/{user}/{service}/{label}/{key}` itself.
  const [volumePaths, setVolumePaths] = useState<Record<string, string>>({})
  // The compose's container target per volume key — the second column of the
  // MOUNT TABLE the panel shows (`host path : container path`). Also the
  // -api's fact; the dashboard never parses the compose.
  const [volumeTargets, setVolumeTargets] = useState<Record<string, string>>({})
  // The sub-service that mounts each volume key (the row label; the key itself
  // is an implementation detail the operator does not need to see), and the
  // rows in service order.
  const [volumeServices, setVolumeServices] = useState<Record<string, string>>({})
  const [volumeRows, setVolumeRows] = useState<{ key: string; service: string; target: string }[]>([])

  // True once a compose has been selected AND previewed — the mount table is
  // only meaningful from then on.
  const [volumePreviewed, setVolumePreviewed] = useState(false)

  // Helper: parse service value which may include recipe path (format: "name@@recipe_path")
  const parseServiceValue = (val: string): { baseName: string; recipePath: string } => {
    const idx = val.indexOf('@@')
    return idx >= 0 ? { baseName: val.slice(0, idx), recipePath: val.slice(idx + 2) } : { baseName: val, recipePath: '' }
  }

  // Fetch expected volumes from the converter's in-call src→key mapping via
  // the lightweight compose-preview response (design §Implementation notes
  // L284-286) — keys are NEVER parsed from .j2 templates in the frontend.
  const fetchExpectedVolumes = async (
    serviceName: string,
    sourcesList?: { name: string; files: string[] }[],
    userName?: string,
    label?: string,
  ) => {
    try {
      const svcVal = serviceName || ''
      // Reset stale volume rows whenever the service changes — the mapping
      // is user+service+label-dependent and must never survive a selection
      // change (design §8.2).
      setExpectedVolumeKeys([])
      setVolumePaths({})
      setVolumeTargets({})
      setVolumeServices({})
      setVolumeRows([])
      setVolumePreviewed(false)
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
        //
        // ONE file, not every plain candidate: this branch runs before the
        // stored file set loads. Merging overlays (fix/test/compose) produced a
        // service set with NO volumes, so the next preview returned zero keys
        // and cleared the rows (measured 2026-10-24). The first plain compose
        // is the base; the stored selection replaces it a moment later.
        const plain = candidates.filter((f: string) => isComposeName(f) && !f.endsWith('.j2'))
        if (plain.length > 0) {
          composeFiles = [pathFor(plain[0])]
        } else {
          const composeJ2 = candidates.find((f: string) => f.endsWith('.yml.j2'))
          if (composeJ2) composeFiles = [pathFor(composeJ2)]
        }
      }
      if (composeFiles.length === 0) { setExpectedVolumeKeys([]); return }

      const u = userName !== undefined ? userName : (form.getFieldValue('user_name') || '')
      const l = label !== undefined ? label : (form.getFieldValue('label') || '0')
      const { data } = await getComposePreview(
        baseName, composeFiles, recipePath, u, String(l), selection.profiles,
      )
      setExpectedVolumeKeys(data.volume_keys || [])
      setVolumePaths(data.volume_paths || {})
      setVolumeTargets(data.volume_targets || {})
      setVolumeServices(data.volume_services || {})
      setVolumeRows(data.volume_rows || [])
      setVolumePreviewed(true)
    } catch {
      setExpectedVolumeKeys([]); setVolumePaths({}); setVolumeTargets({})
      setVolumeServices({}); setVolumeRows([]); setVolumePreviewed(false)
    }
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
        // B2: a free-standing open starts from a clean generation view. A
        // resumed one (`check`) re-attaches below and legitimately shows its
        // pause prompt.
        if (resumeJobId == null) {
          genJob.reset()
          setGenPopupOpen(false)
        }
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
        setBaseRefs(EMPTY_BASE_REFS)
        setClassificationChoice('')
        setRelationChoices({})
        setClassificationState(null)
        setBasePopupOpen(false)
        setContextPopupOpen(false)
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
        // pause popup (compose conflict / external credential) in this panel —
        // AND OPEN that popup. Attaching without opening it left the panel
        // showing an ordinary deploy form while the pause it had just attached
        // to was nowhere on screen, so the operator's only route to the answer
        // was the Tasks page. Measured 2026-10-11 on the `check` path with a
        // credential pause. `renderExternalPrompt` lives inside the popup, so
        // attaching and opening it are one action, not two.
        if (resumeJobId != null) {
          genJob.resumeJob(resumeJobId)
          setGenPopupOpen(true)
        }
        try {
          const { data: cfg } = await client.get('/llm/config')
          const active = !!(cfg && (cfg.is_active || cfg.byok_configured))
          setLlmActive(active)
          // S13 — checked by default exactly when the LLM is available; a
          // no-LLM panel starts unchecked and the box is greyed.
          setLlmValidateEnv(active)
        } catch { setLlmActive(false); setLlmValidateEnv(false) }
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
          // T8/R9 — the Domain's editability follows the TLS state; seed it.
          if (f.https) setHttpsEnabled(true)
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
        // The picker is guarded by the DEPLOYING user, so it loads after the
        // user field is seeded.
        await loadCrossTargets(form.getFieldValue('user_name'))
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

  // F4/S19 — a TLS Retry seeds `ssl_domain` from the certificate it was created
  // with, matched by `initialOptions.fullchain` once the certificate list has
  // loaded. Without this the new "TLS on + no certificate ⇒ Deploy disabled"
  // clause would permanently block every TLS Retry (the form only carries
  // `fullchain`/`privkey`, never `ssl_domain`).
  useEffect(() => {
    if (!open || !initialOptions?.https || !initialOptions?.fullchain) return
    if (sslDomains.length === 0) return
    if (form.getFieldValue('ssl_domain')) return
    const cert = sslDomains.find(d => d.fullchain_path === initialOptions.fullchain)
    if (!cert) return
    setSelectedSslDomain(cert.domain)
    form.setFieldsValue({
      ssl_domain: cert.domain,
      domain: cert.domain,
      fullchain: cert.fullchain_path,
      privkey: cert.privkey_path,
    })
  }, [open, sslDomains, initialOptions])

  const loadDeployableUsers = async () => {
    try {
      const { data } = await client.get('/auth/users/deployable')
      setDeployableUsers((data.users||[]).map((u:any)=>({
        id: u.id, username: u.username, label: u.username,
      })))
    } catch { /* ignore */ }
  }

  // G102 — the deploy panel's dependency picker, grouped by owner account and
  // guarded exactly like the service card's `Can call` picker: the deploying
  // user's own account (same-owner is included), accounts with an active
  // account grant, and public accounts. The selection is the deploy's
  // dependency intent; the gateway writes the grant rows before the app starts.
  const loadCrossTargets = async (usernameArg?: string) => {
    const username = usernameArg || form.getFieldValue('user_name')
    if (!username) { setCrossTargets([]); return }
    try {
      const [{ data: servicesData }, { data: usersData }] = await Promise.all([
        client.get('/services'),
        client.get('/auth/users/deployable'),
      ])
      const deploying = (usersData.users || []).find((u:any)=>u.username === username)
      const activeShared = new Set<string>()
      if (deploying?.id) {
        try {
          const { data: grantsData } = await client.get(`/auth/users/${deploying.id}/grants`)
          for (const g of (grantsData.grants || [])) {
            if (g.state === 'active') activeShared.add(g.shared_username)
          }
        } catch { /* not an admin view — only public accounts remain listable */ }
      }
      const groups: {key:string,label:string,options:{value:string,label:string}[]}[] = []
      for (const group of (servicesData.users || [])) {
        const owner = group.user_name || group.owner_username || ''
        // G102 — same-owner targets are listed like any other dependency.
        if (!owner) continue
        if (!(owner === username || activeShared.has(owner) || group.is_public === true)) continue
        const options: {value:string,label:string}[] = []
        for (const bucket of ['healthy_services','unhealthy_services','missing_services','failed_services']) {
          for (const svc of (group[bucket] || [])) {
            if (svc.status !== 'running') continue
            options.push({
              value: svc.service_id,
              label: `${svc.service_name}:${svc.label}`,
            })
          }
        }
        if (!options.length) continue
        options.sort((a,b)=>a.label.localeCompare(b.label))
        groups.push({
          key: owner,
          label: `${owner}${group.is_public ? ' · Public' : ''}`,
          options,
        })
      }
      groups.sort((a,b)=>a.label.localeCompare(b.label))
      setCrossTargets(groups)
    } catch { setCrossTargets([]) }
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
  // R19 — refresh the CANDIDATE lists without resetting the selection.
  // `loadFileSets` restores the STORED default, which is right when the
  // service changes and wrong right after a generation: the files this run
  // just wrote are not in the candidates the panel fetched earlier, so a
  // generated serving-ports/nginx artifact could be selected in state but not
  // offered in its picker (measured 2026-10-27, e2e-confs generation).
  const refreshFileSetCandidates = async (baseName: string, recipePath: string) => {
    try {
      const { data } = await client.get(`/projects/${baseName}/file-sets`)
      const rp = recipePath || '.'
      setFileSetCandidates({
        ...((data.candidates || {})[rp] || EMPTY_CANDIDATES),
        provenance: (data.provenance || {})[rp] || {},
      })
      setFileSetStale((data.stale || {})[rp] || {})
    } catch { /* a stale candidate list must not fail the generation path */ }
  }

  // `noEnvHint` (S11): the recipe reported `needs_env:false`. It is a HINT that
  // auto-selects the deliberate env `None` for an empty env slot — never a lock,
  // and never an override of an env file the stored selection already names.
  const loadFileSets = async (baseName: string, recipePath: string, noEnvHint = false) => {
    setFileSetLoading(true)
    try {
      const { data } = await client.get(`/projects/${baseName}/file-sets`)
      const rp = recipePath || '.'
      const entry = (data.file_sets || {})[rp] || {}
      setFileSetCandidates({
        ...((data.candidates || {})[rp] || EMPTY_CANDIDATES),
        provenance: (data.provenance || {})[rp] || {},
      })
      setFileSetStale((data.stale || {})[rp] || {})
      setSelection({
        compose: entry.compose || [],
        nginx: entry.nginx || null,
        stream_conf: entry.stream_conf || null,
        mail_conf: entry.mail_conf || null,
        // R1.1/R1.3: the serving-ports artifact and its deliberate-None state,
        // read from the SAME stored file set the other categories come from — so
        // the deploy forwards what the operator (or the generation) selected.
        serving_ports: entry.serving_ports || null,
        serving_ports_none: entry.serving_ports_none || false,
        env: entry.env || [],
        // S11 — with no stored env and a `needs_env:false` hint, the deliberate
        // `None` (send no env files) is pre-selected once. A stored env list
        // wins; the operator can change either afterwards.
        env_none: Array.isArray(entry.env) && entry.env.length > 0
          ? false
          : !!noEnvHint,
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
        // G55 — the per-category map is the record's shape; the legacy singular
        // is still understood for an older refusal.
        const sentConfs = initialOptions.nginx_confs || {}
        setSelection(prev => ({
          compose: Array.isArray(sentCompose) && sentCompose.length ? sentCompose : prev.compose,
          nginx: sentConfs.http || initialOptions.nginx_conf_file_path || prev.nginx,
          stream_conf: sentConfs.stream || prev.stream_conf,
          mail_conf: sentConfs.mail || prev.mail_conf,
          env: Array.isArray(sentEnv) && sentEnv.length ? sentEnv : prev.env,
          // A SENT env list is a real selection — it clears the `needs_env:false`
          // auto-None; otherwise keep whatever the stored file set decided.
          env_none: Array.isArray(sentEnv) && sentEnv.length ? false : prev.env_none,
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
          stream_conf: slot === 'stream_conf' ? null : prev.stream_conf,
          env: slot === 'env' ? [] : prev.env,
          // Regenerating the env is `generate new`, never `None`.
          env_none: slot === 'env' ? false : prev.env_none,
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
  // C16 — the additional context is a shared per-service store: the project
  // page's `add to context` button writes the same value this panel sends.
  const deployContextService = watchedService
    ? parseServiceValue(watchedService).baseName : ''
  const additionalContext = useServiceContext(deployContextService)
  const setAdditionalContext = (ctx: AdditionalContext) => {
    if (deployContextService) setServiceContext(deployContextService, ctx)
  }
  useEffect(() => {
    if (!open || !watchedService) return
    const { baseName, recipePath } = parseServiceValue(watchedService)
    let cancelled = false
    // R9 — with Compose on `generate new`, the Compose BASE REFS are the
    // compose whose profiles gate the optional services; otherwise the sent
    // selection is. A profile cannot be chosen without a compose to derive it
    // from.
    const composeForProfiles = selection.compose.length > 0
      ? selection.compose
      : baseRefs.compose
    if (composeForProfiles.length === 0) {
      setFileSetCandidates(prev => ({ ...prev, profiles: [] }))
      return
    }
    deriveProfiles(baseName, composeForProfiles, recipePath)
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
  }, [open, watchedService, selection.compose, baseRefs.compose])

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
      const recipeNeedsEnv = data.needs_env !== false
      setNeedsEnv(recipeNeedsEnv)
      // S11 — `needs_env:false` auto-selects the deliberate env `None` once for
      // an empty env slot; it is only a hint (the operator can change it).
      loadFileSets(baseName, recipePath, !recipeNeedsEnv)
    } catch (err: any) {
      setCheckError(err?.message || 'Check failed — try again')
      setMissingFiles([])
    } finally { setCheckingMissing(false) }
  }

  // R9 — the selected profiles are GENERATION context (the classifier may pick
  // a profile-matching example) as well as deploy options. The register
  // payload still carries them only as the deploy option.
  const generationSelection = (): FileSetSelectionData => effectiveSelection()

  const effectiveSelection = (): FileSetSelectionData => {
    const st = fileSetStale || {}
    const env = selection.env.filter(p => !(st.env || []).includes(p))
    return {
      compose: selection.compose.filter(p => !(st.compose || []).includes(p)),
      nginx: selection.nginx && !(st.nginx || []).includes(selection.nginx) ? selection.nginx : null,
      // G55 — one file per category; stale entries are kept + flagged, never sent.
      stream_conf: selection.stream_conf
        && !(st.stream_conf || []).includes(selection.stream_conf)
        ? selection.stream_conf : null,
      mail_conf: selection.mail_conf
        && !(st.mail_conf || []).includes(selection.mail_conf)
        ? selection.mail_conf : null,
      env,
      profiles: selection.profiles.filter(p => !(st.profiles || []).includes(p)),
      // R2-F39 — the serving-ports slot MUST travel with the payload. It was
      // rebuilt here by hand and this function forgot it, so the deploy carried
      // no artifact: `produce_for_deploy(serving_ports_path=None)` produced no
      // stream conf, the entry recorded `stream_sub_services: []`, and the card
      // offered no action — with a picker, a stored selection and a working
      // gateway, and NOTHING visible saying why. (The same omission as the save
      // path's, R2-F33: a hand-rebuilt key list is the recurring shape of this
      // bug.)
      serving_ports: selection.serving_ports
        && !(st.serving_ports || []).includes(selection.serving_ports)
        ? selection.serving_ports
        : null,
      // R1.3 — and the deliberate-None state goes with it, never both.
      serving_ports_none: !selection.serving_ports && !!selection.serving_ports_none,
      // S11 — the env counterpart: `None` only when no (effective) env file is
      // selected; a real list and the flag can never both be sent.
      env_none: env.length === 0 && !!selection.env_none,
    }
  }

  // §6: the alert and the Generate button speak ONLY about this panel's
  // selections — never about what the recipe happens to contain. `generate new`
  // is the absence of a selection, so "ready to deploy" means every slot either
  // names a specific file or is deliberately empty.
  const eff = effectiveSelection()
  // F5/S11 — the env slot is satisfied by ANY of:
  //   - a selected env file / ordered list (it IS the deploy env now, §14.1),
  //   - the deliberate `None` (send no env files), or
  //   - the per-user `.raw` handoff this path already produced.
  // `needs_env:false` is only a hint (it pre-selects `None`); it does not enter
  // this predicate.
  const envSatisfied = eff.env.length > 0 || !!eff.env_none || !!perUserEnv
  const effectiveBaseRefs = activeBaseRefs(baseRefs, eff)
  // G56 — with TLS on (and a serving-ports artifact in play), the stream conf is
  // a SLOT like the http one: a fully-configured deploy names both. `None` is
  // the deliberate no-stream state and demands nothing.
  const httpsOn = !!Form.useWatch('https', form)
  // F4/S14 — the Deploy gate follows the FORM value, not the `httpsEnabled`
  // state (which is never reset on a fresh open). With TLS on and no
  // certificate selected there is no trusted domain, so Deploy is disabled.
  const watchedSslDomain = Form.useWatch('ssl_domain', form)
  // 2026-11-19/T3 — the stream slot is derived from the SELECTED artifact's
  // rows, never the TLS toggle alone. The summary is fetched whenever the
  // selection/service changes; a missing, unreadable or malformed artifact
  // reads as "no rows", so the line never demands a conf it cannot produce.
  const [streamRowCount, setStreamRowCount] = useState<number | null>(null)
  useEffect(() => {
    if (!open || !watchedService || !eff.serving_ports || eff.serving_ports_none) {
      setStreamRowCount(null)
      return
    }
    const { baseName, recipePath } = parseServiceValue(watchedService)
    if (!baseName) { setStreamRowCount(null); return }
    let cancelled = false
    client.get(`/projects/${baseName}/serving-ports/summary`, {
      params: { path: eff.serving_ports, recipe_path: recipePath },
    }).then(({ data }) => {
      if (!cancelled) {
        setStreamRowCount(data?.readable ? (Number(data.stream_rows) || 0) : null)
      }
    }).catch(() => { if (!cancelled) setStreamRowCount(null) })
    return () => { cancelled = true }
  }, [open, watchedService, eff.serving_ports, eff.serving_ports_none])
  const streamSlotNeeded =
    httpsOn && !eff.serving_ports_none && (streamRowCount ?? 0) > 0
  // R26–R29 — the classification/relation picker state. The endpoint is the
  // read-only twin of the gateway's intake check: the matched variants, the
  // coverage/target checks, the 1:1 projection and the overwrite preview.
  const watchedDeps = Form.useWatch('dependency_service_ids', form)
  const depKey = ((watchedDeps as string[] | undefined) || []).filter(Boolean).join(',')
  const envKey = eff.env.join(',')
  const composeKey = eff.compose.join(',')
  const profileKey = (eff.profiles || []).join(',')
  useEffect(() => {
    if (!open || !watchedService) { setClassificationState(null); return }
    const { baseName, recipePath } = parseServiceValue(watchedService)
    if (!baseName) { setClassificationState(null); return }
    const envNames = eff.env_none ? [] : envKey.split(',').filter(Boolean)
    const currentRelations = Object.entries(relationChoices)
      .map(([t, m]) => `${t}=${m}`).join(',')
    let cancelled = false
    client.get(`/projects/${baseName}/classification-state`, {
      params: {
        dependency_service_ids: depKey,
        env: envNames.join(','),
        env_none: !!eff.env_none,
        compose: composeKey,
        profiles: profileKey,
        recipe_path: recipePath,
        current: classificationChoice,
        current_relations: currentRelations,
      },
    }).then(({ data }) => {
      if (!cancelled) setClassificationState(data)
    }).catch(() => { if (!cancelled) setClassificationState(null) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, watchedService, depKey, envKey, eff.env_none, composeKey, profileKey,
      classificationChoice, classificationRefresh])
  const clsState = classificationState
  const clsVariants: any[] = clsState?.classification?.variants || []
  const clsVariantKeys = clsVariants.map((v: any) => v.value).join('|')
  const clsDefault: string = clsState?.classification?.default || 'generate_new'
  // A red variant is never the automatic default (R26: "matched valid
  // variant, else generate new"); the endpoint already applies this, and this
  // is the defensive twin so a future state change cannot select one silently.
  const clsDefaultSafe = clsVariants.find((v: any) => v.value === clsDefault)?.red
    ? 'generate_new'
    : clsDefault
  // C20 — no stale selection state: a variant chosen under another
  // fingerprint is not in the matched list, so the explicit choice is dropped
  // and the picker falls back to the current default.
  useEffect(() => {
    if (!classificationChoice || classificationChoice === 'none'
        || classificationChoice === 'generate_new') return
    if (clsState && !clsVariants.some((v: any) => v.value === classificationChoice)) {
      setClassificationChoice('')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [classificationChoice, clsVariantKeys, clsState])
  const envIsNone = !!eff.env_none
  // The picker's effective value: the operator's choice, else the default the
  // gateway computed (a matched valid variant, else generate new). Under the
  // deliberate env `None` there is no classification at all.
  const effectiveClassification = envIsNone
    ? 'none'
    : (classificationChoice || clsDefaultSafe)
  const clsIsExisting = effectiveClassification.startsWith('env.classification.')
  const clsSelectedVariant = clsVariants.find((v: any) => v.value === effectiveClassification)
  const relationStates: any[] = clsState?.relations || []
  const relationModeFor = (target: string) =>
    relationChoices[target] || relationStates.find(r => r.target === target)?.default
      || 'generate_new'
  const relationsPayload: Record<string, string> = Object.fromEntries(
    relationStates.map(r => [r.target, relationModeFor(r.target)]))
  const clsCoverageMissing: string[] =
    clsIsExisting ? (clsState?.classification?.coverage_missing || []) : []
  const clsTargetViolations: any[] =
    clsIsExisting ? (clsState?.classification?.target_violations || []) : []
  const clsInternalViolations: any[] =
    clsIsExisting ? (clsState?.classification?.internal_violations || []) : []
  const overwritePreview: string[] =
    clsIsExisting && eff.env.length > 0
      ? (clsState?.classification?.overwrite || []) : []
  const depDuplicate = !!clsState?.classification?.duplicate_targets
  const slotsSpecified = eff.compose.length > 0 && !!eff.nginx && envSatisfied
    && (!streamSlotNeeded || !!eff.stream_conf)
  // F5 — the readiness line names the ACTUAL missing slots/files, above the
  // Generation prompt. It used to print "Missing essential files:" with an
  // empty list whenever only the env slot was `generate new`, because that
  // unmet item is a slot, not a file from `check-missing-files`.
  const missingSlotLabels: string[] = []
  if (eff.compose.length === 0) missingSlotLabels.push('compose')
  if (!eff.nginx) missingSlotLabels.push('nginx config')
  if (!envSatisfied) missingSlotLabels.push('env files')
  if (streamSlotNeeded && !eff.stream_conf) missingSlotLabels.push('stream conf')
  const missingLabels = [...new Set([...missingSlotLabels, ...missingFiles])]

  // Async generation job (ordered phases compose → nginx → env, one job).
  const genJob = useGenerationJob()
  const [genValidations, setGenValidations] = useState<Record<string, any>>({})
  // The manual Generate path resets `genJob` when it moves into the review
  // gate, so the export must keep the last non-null job snapshot (id, status,
  // result) instead of reading an already-cleared hook state.
  const [lastGeneration, setLastGeneration] = useState<any>(null)
  // Deploy progress popup (design §8.3) — shows generation steps + deploy step.
  const [genPopupOpen, setGenPopupOpen] = useState(false)

  // B2 / G58 — closing the panel ENDS its attachment to the generation leg.
  // `genJob` and `genPopupOpen` survived the close (only `handleDeploy`'s
  // finally or the prompt's own Close cleared them), so the next, unrelated
  // Deploy panel rendered the previous leg's "waiting for external
  // credentials" prompt inline and kept its hidden pause modal mounted.
  // The server-side pause is untouched: re-opening `check` still attaches to
  // it, because that path calls `resumeJob` again.
  useEffect(() => {
    // `handleDeploy` closes the panel ITSELF right after snapshotting the form,
    // then runs the generation + deploy from the popup. That close is not a
    // detach: resetting here killed the auto-run (the job went on server-side
    // and the dashboard stopped polling it).
    if (!open && !deployAutoRunRef.current) {
      genJob.reset()
      setGenPopupOpen(false)
    }
  }, [open])
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
      // R1/R11 — Generate is the workflow's SECOND operation: the intake
      // records `operation=generate` on the shared record and the completion
      // stops at the review gate. The generation leg is still polled through
      // the task read model.
      const { data } = await client.post('/services/generate', {
        user_name: form.getFieldValue('user_name') || '',
        service_name: baseName,
        project_root: recipePath ? `${baseName}/${recipePath}` : baseName,
        recipe_path: recipePath,
        label: form.getFieldValue('label') || '0',
        domain: form.getFieldValue('domain') || 'localhost',
        passwd: form.getFieldValue('passwd') || '',
        https: !!form.getFieldValue('https'),
        // G20 — the session this job opens must record the proxy the panel is
        // showing, or a `check` restart replays `false` (see
        // `effectiveUseGlobalProxy`).
        use_global_proxy: effectiveUseGlobalProxy(),
        selection: generationSelection(),
        prompt,
        base_refs: effectiveBaseRefs,
        context: additionalContext,
        // The Generate preview must carry the selected dependencies too: the
        // env classifier needs the identities/relation files, and the deploy
        // path already sends them. Without this a dependency-only run fails the
        // category check with an unmapped variable.
        dependency_service_ids: form.getFieldValue('dependency_service_ids') || [],
        // R26–R28 — the picker selections; the force control is retired (C21).
        classification: effectiveClassification,
        relations: relationsPayload,
        // G25 — the NAMING follows the TRIGGER: on a guided restart the
        // generation emits `.fix` files, not `.base`.
        ...(fixingGuide ? { fix: true } : {}),
      })
      if (data.generation_task_id) {
        genJob.resumeJob(data.generation_task_id)
      }
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
  // Generation job completed → move the result into the review gate. On the
  // Deploy auto-run path (popup open) the completed events are KEPT so the popup
  // freezes on the final frame; the manual Generate path resets into the review
  // gate with the "review, then deploy" toast (feature 4).
  useEffect(() => {
    const st = genJob.jobState
    if (!st) return
    setLastGeneration({
      job_id: genJob.jobId,
      status: st.status,
      phase: st.phase,
      phase_index: st.phase_index,
      phase_total: st.phase_total,
      progress: st.progress,
      result: st.result || null,
    })
  }, [genJob.jobState, genJob.jobId])

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
        if (typeof resultSel.stream_conf === 'string' && resultSel.stream_conf) next.stream_conf = resultSel.stream_conf
        if (typeof resultSel.mail_conf === 'string' && resultSel.mail_conf) next.mail_conf = resultSel.mail_conf
        // The serving-ports slot was generated by this run too, and the
        // gateway swapped it in its reported selection — adopt it like every
        // other slot (it used to be missing from this hand-rebuilt list).
        if (typeof resultSel.serving_ports === 'string' && resultSel.serving_ports) {
          next.serving_ports = resultSel.serving_ports
          next.serving_ports_none = false
        }
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
      const producedNginx = Object.keys(files).find(n => n.endsWith('.conf') && !n.endsWith('.j2')
        && confCategory(n) === 'http')
      if (producedNginx) setSelection(prev => ({ ...prev, nginx: producedNginx }))
      const producedStream = Object.keys(files).find(n => n.endsWith('.conf') && !n.endsWith('.j2')
        && confCategory(n) === 'stream')
      if (producedStream) setSelection(prev => ({ ...prev, stream_conf: producedStream }))
      const producedPorts = Object.keys(files).find(
        n => /^serving[_-]ports.*\.ya?ml$/.test(n.split('/').pop() || n)
      )
      if (producedPorts) {
        setSelection(prev => ({ ...prev, serving_ports: producedPorts, serving_ports_none: false }))
      }
    }
    // The files exist now; re-read the candidates so every picker can OFFER
    // what this run produced.
    {
      const svcVal = form.getFieldValue('service_name') || ''
      const { baseName, recipePath } = parseServiceValue(svcVal)
      if (baseName) void refreshFileSetCandidates(baseName, recipePath)
    }
    if (!isDeployAuto) genJob.reset()
  }, [genJob.jobState])

  // Save an edited generated file back to state + disk
  // §5b: open a SELECTED file for view/edit. Fetch it from the recipe through
  // the same project-files endpoint the generated-file editor uses.
  const openSelectedFile = async (name: string, fork = true) => {
    const svcVal = form.getFieldValue('service_name') || ''
    const { baseName, recipePath } = parseServiceValue(svcVal)
    const path = recipePath ? `${recipePath}/${name}` : name
    try {
      const { data } = await client.get(`/projects/${baseName}/files/${path}`)
      setEditorFileName(name)
      setEditorContent(typeof data?.content === 'string' ? data.content : '')
      // T6 — a generated file is edited IN PLACE; only a picker-selected
      // source forks to `.fix` on save. The job result is metadata only, so
      // the content always comes from this fetch, never from the result.
      setEditorFork(fork)
      setEditorModalOpen(true)
    } catch (err: any) {
      message.error(err.response?.data?.detail || `Could not open ${name}`)
    }
  }

  const saveEditorFile = async () => {
    if (!editorFileName) return
    setEditorSaving(true)
    try {
      const targetName = editorFork ? userVariantFor(editorFileName) : editorFileName
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
          stream_conf: prev.stream_conf === editorFileName ? targetName : prev.stream_conf,
          mail_conf: prev.mail_conf === editorFileName ? targetName : prev.mail_conf,
          env: prev.env.map(p => (p === editorFileName ? targetName : p)),
          // R4-F64 — and the serving-ports slot, which this list forgot. Every
          // other category is flipped here; `serving_ports` was added to the
          // selection later and this hand-rebuilt list was not carried with it,
          // so an edit to the ports artifact forked to `.fix` and left the
          // picker on the ORIGINAL — the next deploy then used the file the
          // operator had just edited away from. The fourth instance of the same
          // shape (F39, F33, F62), which is why the comment is repeated rather
          // than referenced: a hand-rebuilt key list is how this keeps happening.
          serving_ports: prev.serving_ports === editorFileName ? targetName : prev.serving_ports,
        }))
        message.success(`Saved as ${targetName} — the selection now uses it`)
      } else {
        message.success(`${editorFileName} saved`)
      }
      setEditorModalOpen(false)
      setEditorFork(false)
      // Re-run the classification/relation checks against the saved bytes.
      setClassificationRefresh(n => n + 1)
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to save file')
    } finally {
      setEditorSaving(false)
    }
  }

  const handleDeploy = async (values: any) => {
    setLoading(true)
    // R11 parity: a DIRECT deploy (files complete) shows no "Deploying…"
    // popup — the optimistic Building card appears immediately. The popup is
    // opened below only when the gateway says the workflow is generating.
    setGenPopupOpen(false)
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
    // The submitted triple, captured before the panel closes: it is the
    // placeholder key for the whole call, including every failure path.
    const svcVal = snap.service_name || ''
    const { baseName, recipePath } = parseServiceValue(svcVal)
    const deployLabel = String(snap.label || '0')
    // Close the deploy panel immediately — the Deploying… popup is the UI from
    // here on (it lives in this component, which stays mounted).
    onClose()
    // S22 — a SESSION pause (the LLM env guard) is answered from the service
    // card's Check, not by this popup. Set when the poll below sees one, so the
    // catch reports it as a pause instead of an error.
    let pausedForCheck = false
    try {
      // Snapshot previously generated files; the Deploy auto-run below adds
      // anything generated during THIS call and returns it synchronously
      // (React state is not visible mid-async-flow).
      const sel = effectiveSelection()

      // The UI guard stays (parity); the gateway refuses the same intent as a
      // structured 4xx (R6). Only the NO-COMPOSE case is blocked here: a
      // missing serving_ports/nginx slot is exactly what the gateway's
      // generation fills, so it must not stop the intent.
      if (sel.compose.length === 0 && !prompt && !genJob.jobState) {
        message.error('Cannot deploy: no compose file is selected.')
        setLoading(false)
        setGenPopupOpen(false)
        deployAutoRunRef.current = false
        return
      }
      // T8/R9 — with TLS on the Domain is certificate-driven; a deploy with no
      // certificate has no trusted domain and is refused here as well as by the
      // form rule.
      if (snap.https && !snap.ssl_domain) {
        message.error('Cannot deploy: select an SSL certificate first.')
        setLoading(false)
        setGenPopupOpen(false)
        deployAutoRunRef.current = false
        return
      }
      // The client guard passed: this submission creates a card, so the page
      // shows its blinking placeholder NOW — before the POST below.
      onDeployStarted(snap.user_name, baseName, deployLabel)

      // Thin migration R1/R11 — the client sends the INTENT; the gateway owns
      // validation, label reservation, generation, pauses, finalization, the
      // claim and the register. The browser no longer orchestrates the
      // generation -> deploy handoff.
      const payload: any = {
        user_name: snap.user_name,
        service_name: baseName,
        project_root: recipePath ? `${baseName}/${recipePath}` : baseName,
        recipe_path: recipePath || '',
        label: deployLabel,
        domain: snap.domain || 'localhost',
        passwd: snap.passwd || '',
        https: snap.https || false,
        use_global_proxy: snap.use_global_proxy || false,
        // A persisted stream conf is not applicable when TLS is off (the
        // gateway refuses exactly that intent: R6). The old panel dropped it
        // from the -api payload; the intent must drop it too.
        // `env_none` travels inside the selection; the flag is effective only
        // for a real selected env (never under `None`/`generate new`).
        selection: { ...sel, stream_conf: snap.https ? sel.stream_conf : null },
        prompt,
        base_refs: effectiveBaseRefs,
        context: additionalContext,
        // R26–R28 — the classification/relation selections; no force (C21).
        classification: effectiveClassification,
        relations: relationsPayload,
        // S13 — the LLM env guard flag, top-level on the deploy payload. It
        // applies only when a selected env list is actually being sent.
        llm_validate_env: !!(llmActive && llmValidateEnv && sel.env.length > 0 && !sel.env_none),
        ...(fixingGuide ? { fix: true } : {}),
      }
      if (snap.https) {
        payload.fullchain = snap.fullchain || ''
        payload.privkey = snap.privkey || ''
      }
      // Volume mapping — only the operator's OVERRIDES (G60/D8).
      if (snap.volumes && snap.volumes.length > 0) {
        const overrides: Record<string, string> = {}
        for (const v of snap.volumes) {
          if (v.key && v.value && v.value !== volumePaths[v.key]) overrides[v.key] = v.value
        }
        if (Object.keys(overrides).length > 0) payload.volumes = overrides
      }
      if (snap.build_args && snap.build_args.length > 0) {
        payload.build_args = {}
        for (const a of snap.build_args) {
          if (a.key && a.value) payload.build_args[a.key] = a.value
        }
      }
      // G102 — the dependency selection; the gateway writes the pre-start
      // grant rows and starts the app after the resolver sync.
      if (snap.dependency_service_ids && snap.dependency_service_ids.length > 0) {
        payload.dependency_service_ids = snap.dependency_service_ids
      }

      const { data } = await client.post('/services/deploy', payload)
      const workflowId = data.workflow_id || data.service_id
      if (!workflowId) throw new Error('the gateway did not return a workflow handle')

      // A direct deploy is already handed off: no generation popup (R11/§11
      // step 3). Only a generating workflow gets the progress popup.
      if (['deploying', 'building', 'running'].includes(data.stage)) {
        message.success('Deploy queued!')
        onDeployed(data.deploy_task_id || data.task_id || '', snap.user_name,
                   baseName, String(data.label || deployLabel))
        onClose()
        return
      }
      setGenPopupOpen(true)

      // The popup renders the gateway's workflow read model. A pause is
      // answered through the same inline prompt, with values only (R4).
      genJob.adoptJob({ status: 'running', progress: 'starting…', events: [] }, null)
      for (let i = 0; i < 900; i++) {
        await new Promise(r => setTimeout(r, 1000))
        const { data: wf } = await client.get(`/services/workflows/${workflowId}`)
        const gen = wf.generation
        if (gen) genJob.adoptJob(gen, gen.task_id || gen.id || null)
        if (wf.stage === 'paused') {
          // A GENERATION-leg pause is answered by the inline PausePrompt in
          // this popup, so keep polling. A SESSION pause (the LLM env guard,
          // S22) has no job pause to answer: its surface is the service
          // card's Check, so stop instead of polling a pause forever.
          const genPaused = !!gen && (gen.status === 'paused'
            || gen.status === 'awaiting_input')
          if (genPaused) continue
          pausedForCheck = true
          throw new Error(wf.detail || 'The deploy is paused for a fix.')
        }
        if (wf.stage === 'refused') {
          // A refusal accepted nothing: the placeholder must not linger and
          // claim a deployment that never started (R4.1).
          onDeployFailed(snap.user_name, baseName, deployLabel)
          setAtCapacity({ code: wf.reason || 'at_capacity', detail: wf.detail || '' })
          return
        }
        if (wf.stage === 'failed') {
          throw new Error(wf.detail || gen?.error || 'Deploy failed')
        }
        if (wf.stage === 'deploying' || wf.stage === 'building' || wf.stage === 'running') {
          message.success('Deploy queued!')
          onDeployed(wf.deploy_task_id || gen?.task_id || '', snap.user_name,
                     baseName, String(wf.label || deployLabel))
          onClose()
          return
        }
      }
      throw new Error('Deploy timed out')
    } catch (err: any) {
      // Every rejection removes the waiting card; the error toast below is the
      // page's existing one (R4.1).
      onDeployFailed(snap.user_name, baseName, deployLabel)
      // S22 — a guard pause is not an error: the paused session card (with the
      // guide and Check) is the surface, so report it as a notice and stop.
      if (pausedForCheck) {
        message.info('The deploy is paused for an env fix — open Check on the service card, confirm the fixing prompt and deploy again')
        return
      }
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
        // Read the WHOLE ladder, not just `detail`-as-a-string.
        //
        // A FastAPI `HTTPException(409, {...})` serialises as
        // `{"detail": {"error": …, "detail": …}}`, so `d` here is an OBJECT for
        // every structured refusal this path can meet — and the old
        // `typeof d === 'string' ? d : 'Deploy failed'` threw the whole body
        // away and printed the literal `Deploy failed`. That is exactly what the
        // operator reported on 2026-10-14 ("deployed is failed is shown
        // immediately"), and it named nothing: the gateway's 409
        // `combination_reserved` exists to SAY the instruction ("a pre-deploy
        // session already exists for this (user, service, label) — check, cancel
        // or delete it first"). The `generate` entry point has always unpacked
        // this body; the deploy path was never given the same treatment.
        //
        // The sentence is preferred over the code, and `err.message` is the last
        // resort: the deploy's first act is a generation leg, which goes through
        // `useGenerationJob.startJob` — that FLATTENS the same body into
        // `message` and rethrows with `response` re-attached.
        message.error(
          (typeof d === 'string' && d)
          || (typeof d?.detail === 'string' && d.detail)
          || (typeof d?.error === 'string' && d.error)
          || (typeof err?.message === 'string' && err.message)
          || 'Deploy failed'
        )
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

  // The generated per-user handoff is NAMED for the (user, label) it was made
  // for (`.env.raw.<user>.<label>` — see the env phase's `_pu_env`), so it
  // stops being THIS deployment's handoff the moment either changes. The service
  // picker already clears it (`loadFileSets`), but a user or label change went
  // unnoticed — harmless while an empty env slot always forced a regeneration,
  // and no longer harmless now that `envSatisfied` trusts the handoff: a Deploy
  // for user B would otherwise have carried user A's env file.
  useEffect(() => {
    setPerUserEnv(null)
  }, [selectedUserName, selectedLabel])

  // Ref to access Form.List's add() function from outside the render
  const volumeAddRef = useRef<((defaultValue?: any) => void) | null>(null)

  // G60 (D8) — refetch the preview whenever the TARGET IDENTITY changes: the
  // volume paths are the -api's golden information and depend on
  // (user, service, label), so the panel never derives them itself.
  useEffect(() => {
    if (!open || !selectedServiceName || !selectedUserName) return
    fetchExpectedVolumes(selectedServiceName, undefined, selectedUserName, selectedLabel || '0')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, selectedServiceName, selectedUserName, selectedLabel, selection.compose, selection.profiles])

  // Display the returned rows: editable, prefilled with EXACTLY the -api paths.
  // Re-applied when the Volume Mapping section OPENS, because antd does not
  // mount the Collapse content (and so the Form.List) until then — the fill
  // used to run against a list that did not exist and the section opened empty
  // (measured 2026-10-24, twice).
  const applyVolumeRows = () => {
    if (!selectedUserName) return
    const keys = expectedVolumeKeys
    if (keys.length === 0) return
    // The row carries its target too (the compose fact the -api returned), so
    // the mount table never depends on a separate lookup racing the fill.
    const rows = volumeRows.length > 0
      ? volumeRows
      : keys.map(key => ({ key, service: volumeServices[key] || '', target: volumeTargets[key] || '' }))
    const volumes = rows.map(r => ({
      key: r.key,
      value: volumePaths[r.key] || '',
      target: r.target,
      service: r.service,
    }))
    form.setFieldsValue({ volumes })
    // Also ensure Form.List has the right number of items
    if (volumeAddRef.current) {
      const currentVolumes = form.getFieldValue('volumes') || []
      while (currentVolumes.length < keys.length) {
        volumeAddRef.current()
        currentVolumes.push({})
      }
      setTimeout(() => form.setFieldsValue({ volumes }), 0)
    }
  }
  useEffect(() => {
    applyVolumeRows()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedUserName, expectedVolumeKeys, volumePaths, volumeTargets, volumeServices, volumeRows])

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
          : atCapacity.code === 'queue_full'
          ? "The task queue is full — nothing was queued"
          : atCapacity.code === 'queue_unavailable'
          ? "The task queue is unavailable — nothing was queued"
          : "Platform is at capacity — nothing was queued"}
        description={<>
          {atCapacity.detail || (atCapacity.code === 'queue_unavailable'
            ? 'The planner heartbeat is stale. Retry once it is fresh.'
            : atCapacity.code === 'queue_full'
            ? 'Wait for queued work to start, or cancel a queued task.'
            : atCapacity.code === 'generation_at_capacity'
            ? 'Wait for a running generation to finish, or cancel one.'
            : 'Cancel a task that has been hanging, then retry.')}
          <div style={{marginTop:6}}>
            <Button size="small" onClick={()=>{ onClose(); window.location.href = '/tasks' }}>
              {atCapacity.code === 'generation_at_capacity'
                ? 'Go to Tasks and cancel a running generation'
                : atCapacity.code === 'queue_full'
                ? 'Go to Tasks and cancel a queued task'
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
    // The pause's kinds are dispatched in ONE place (`PausePrompt`): the
    // credential VALUES, the compose conflict's editable GUIDANCE, and R1.3's
    // two validation checks — each of which names the file it wants fixed.
    // Spelled out here it was a two-way branch, so a third kind fell through to
    // the credential prompt and offered a form that could not answer it.
    return (
      <PausePrompt
        jobState={genJob.jobState}
        onSubmit={genJob.submitInput}
        onCancel={_onCancel}
        cancelLabel={_label}
      />
    )
  }

  // Export the panel's selected options and the generation path's output.
  // The generated files' CONTENT is read from the recipe (the job result is
  // metadata only); an edited generated file that is already held in state is
  // exported as-is. The deploy password is redacted and build-arg VALUES are
  // not exported (they can carry proxy credentials); their keys are.
  const exportGenerationJson = async () => {
    const svcVal = form.getFieldValue('service_name') || ''
    const { baseName, recipePath } = parseServiceValue(svcVal)
    const fields = form.getFieldsValue()
    const heldFiles = generatedFiles as Record<string, any>
    const names = Object.keys(heldFiles || {})
    const files: Record<string, any> = {}
    await Promise.all(names.map(async (name) => {
      const held = heldFiles[name]
      if (typeof held === 'string') {
        files[name] = { content: held }
        return
      }
      let content: string | null = null
      try {
        const path = recipePath ? `${recipePath}/${name}` : name
        const { data } = await client.get(`/projects/${baseName}/files/${path}`)
        content = typeof data?.content === 'string' ? data.content : null
      } catch {
        // The metadata still exports; the file may have been removed.
      }
      files[name] = { ...(held && typeof held === 'object' ? held : {}), content }
    }))
    const js = genJob.jobState || lastGeneration || {}
    const payload = {
      exported_at: new Date().toISOString(),
      selected_options: {
        service_value: fields.service_name || '',
        user_name: fields.user_name || '',
        label: fields.label || '',
        domain: fields.domain || '',
        https: !!fields.https,
        ssl_domain: fields.ssl_domain || '',
        use_global_proxy: effectiveUseGlobalProxy(),
        dependencies: fields.dependency_service_ids || [],
        selection: effectiveSelection(),
        base_refs: effectiveBaseRefs,
        context: additionalContext,
        prompt,
        classification: effectiveClassification,
        relations: relationsPayload,
        llm_active: llmActive,
        llm_validate_env: !!llmValidateEnv,
        volumes: Array.isArray(fields.volumes) ? fields.volumes : [],
        build_arg_keys: Array.isArray(fields.build_args)
          ? fields.build_args.map((a: any) => a?.key).filter(Boolean)
          : [],
        passwd: fields.passwd ? '<redacted>' : '',
      },
      generation: {
        job_id: genJob.jobId ?? lastGeneration?.job_id ?? null,
        status: js.status || null,
        phase: js.phase || null,
        phase_index: js.phase_index ?? null,
        phase_total: js.phase_total ?? null,
        progress: js.progress || '',
        env_name: perUserEnv,
        validations: genValidations,
        result: js.result || null,
        files,
      },
    }
    const text = JSON.stringify(payload, null, 2)
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const win = window.open(url, '_blank')
    if (!win) {
      URL.revokeObjectURL(url)
      message.error('The browser blocked the export window — allow pop-ups for this page')
      return
    }
    // Keep the URL alive while the new tab reads it, then release it.
    setTimeout(() => URL.revokeObjectURL(url), 60000)
  }

  // R2/R4 — the prompt box owns the toolbar, the summary lines and exactly
  // ONE compact alert. The old env-only reference control is gone; its role is
  // the Env slot of the base-files popup (R1).
  const renderGenerationPrompt = () => {
    const refRows: { name: string; slot: 'compose' | 'serving_ports' | 'nginx' | 'env'; cat?: 'http' | 'stream' | 'mail' }[] = []
    effectiveBaseRefs.compose.forEach(name => refRows.push({ name, slot: 'compose' }))
    if (effectiveBaseRefs.serving_ports) {
      refRows.push({ name: effectiveBaseRefs.serving_ports, slot: 'serving_ports' })
    }
    ;(['http', 'stream', 'mail'] as const).forEach(cat => {
      const n = effectiveBaseRefs.nginx[cat]
      if (n) refRows.push({ name: n, slot: 'nginx', cat })
    })
    effectiveBaseRefs.env.forEach(name => refRows.push({ name, slot: 'env' }))
    const removeRef = (name: string, slot: 'compose' | 'serving_ports' | 'nginx' | 'env',
                        cat?: 'http' | 'stream' | 'mail') => {
      if (slot === 'compose') {
        setBaseRefs({ ...baseRefs, compose: baseRefs.compose.filter(x => x !== name) })
      } else if (slot === 'env') {
        setBaseRefs({ ...baseRefs, env: baseRefs.env.filter(x => x !== name) })
      } else if (slot === 'nginx') {
        setBaseRefs({ ...baseRefs, nginx: { ...baseRefs.nginx, [cat || 'http']: null } })
      } else {
        setBaseRefs({ ...baseRefs, serving_ports: null })
      }
    }
    return (
      <>
        <Divider plain style={{ margin: '8px 0' }}>Generation prompt</Divider>
        <Input.TextArea
          rows={2}
          disabled={!llmActive}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="Prompt for the generation agent (missing-files generation + deploy-time per-user env). You may include doc URLs — the agent fetches them."
        />
        <Space size="small" style={{ marginTop: 8 }} wrap>
          <Button size="small" onClick={() => setBasePopupOpen(true)}>⚙ base files</Button>
          <Button size="small" onClick={() => setContextPopupOpen(true)}>+ context</Button>
        </Space>
        <Alert
          type="info"
          showIcon
          style={{ marginTop: 8 }}
          message="select a base to put hard constraint on how a file should be like"
        />
        {(refRows.length > 0 || additionalContext.files.length > 0
          || additionalContext.links.length > 0) && (
          <div style={{ marginTop: 8 }}>
            {refRows.map(row => (
              <SelectedFileRow
                key={`ref:${row.slot}:${row.name}`}
                name={row.name}
                onOpen={openSelectedFile}
                onRemove={() => removeRef(row.name, row.slot, row.cat)}
              />
            ))}
            {additionalContext.files.map(name => (
              <SelectedFileRow
                key={`ctx:${name}`}
                name={name}
                onOpen={openSelectedFile}
                onRemove={() => setAdditionalContext({
                  files: additionalContext.files.filter(x => x !== name),
                  links: additionalContext.links,
                })}
              />
            ))}
            {additionalContext.links.map(url => (
              <div key={`link:${url}`} style={{ marginTop: 4 }}>
                <Tag closable onClose={() => setAdditionalContext({
                  files: additionalContext.files,
                  links: additionalContext.links.filter(x => x !== url),
                })}>
                  {url}
                </Tag>
              </div>
            ))}
          </div>
        )}
      </>
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
    {/* R1 — the deploy popup mirrors the panel's FOUR sent-file pickers. */}
    <BaseFilesPopup
      open={basePopupOpen}
      onClose={() => setBasePopupOpen(false)}
      slots={DEPLOY_REF_SLOTS}
      candidates={fileSetCandidates}
      selection={eff}
      refs={baseRefs}
      onChange={setBaseRefs}
      onOpenFile={openSelectedFile}
    />
    {/* R3 — the existing project browser plus a URL input; context only. */}
    <AdditionalContextPopup
      open={contextPopupOpen}
      onClose={() => setContextPopupOpen(false)}
      serviceName={parseServiceValue(watchedService || '').baseName}
      recipePath={parseServiceValue(watchedService || '').recipePath}
      files={additionalContext.files}
      links={additionalContext.links}
      onChange={setAdditionalContext}
      onOpenFile={openSelectedFile}
    />
    <Modal
      title={`Deploy${selectedServiceName ? `: ${selectedServiceName}` : ''}`}
      open={open}
      onCancel={onClose}
      footer={null}
      width={640}
      destroyOnClose
    >
      {/* G58 — the panel is not a login form. It happens to contain password
          inputs (the service password and the external credential), and without
          an explicit opt-out the browser's credential heuristic treats the form
          as one: saved password into the password field, saved username into the
          first unannotated text input (the services-page Filter). */}
      <Form form={form} layout="vertical" onFinish={handleDeploy} preserve={false}
        autoComplete="off"
        // F4/S14 — no default Domain: it is operator input with TLS off and
        // certificate-driven with TLS on. `localhost` was a silent default.
        initialValues={{ label: '0', https: false }}>
        
        {/* Hidden certificate fields live OUTSIDE the Space: as direct Space
            children they rendered zero-height rows, each adding a 16px gap
            (the "3 blank lines" above Volume Mapping). */}
        <Form.Item name="fullchain" hidden><Input /></Form.Item>
        <Form.Item name="privkey" hidden><Input /></Form.Item>
        <Space style={{ width: '100%' }} direction="vertical" size="middle">
          {/* ---- User & Service ---- */}
          <Space.Compact block>
            <Form.Item name="user_name" label="User Name" rules={[{ required: true, message: 'Required' }]} style={{ flex: 1 }}>
              <Select showSearch placeholder="Select registered user" id="deploy-target-user"
                {...({ autoComplete: 'off' } as any)}
                filterOption={(input, option) => (option?.label as string||'').toLowerCase().includes(input.toLowerCase())} options={deployableUsers.map(u=>({value:u.username,label:u.username}))}
                onChange={(val) => {
                  const svc = form.getFieldValue('service_name') || ''
                  const { baseName: svcBase } = parseServiceValue(svc)
                  if (val && svcBase) computeNextLabel(val, svcBase)
                  // G86 — the allowed-services picker is guarded by the user.
                  loadCrossTargets(val)
                }}
              />
            </Form.Item>
            <Form.Item name="service_name" label="Service" rules={[{ required: true, message: 'Required' }]} style={{ flex: 1 }}>
              <Select
                showSearch
                id="deploy-target-service"
                {...({ autoComplete: 'off' } as any)}
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

          {/* ---- Domain setting (identity of this deployment) ---- */}
          {/* Hidden until a source project is chosen: label allocation, the
              TLS certificate list and the service password all describe an
              instance that does not exist yet. */}
          {selectedServiceName && (
            <>
              <Divider plain>Domain setting</Divider>
              <Space.Compact block>
                <Form.Item name="label" label="Label" style={{ flex: 1 }}>
                  <Input disabled addonAfter={computingLabel ? <Spin size="small" /> : null} />
                </Form.Item>
                <Form.Item name="domain" label="Domain" style={{ flex: 2 }}>
                  {/* T8/R9 — with TLS on the Domain is CERTIFICATE-DRIVEN: it is
                      filled from the selected certificate and is not editable.
                      With TLS off it stays free text. */}
                  <Input name="deploy_domain" autoComplete="off"
                    disabled={httpsOn} />
                </Form.Item>
                <Form.Item name="passwd" label="Password" style={{ flex: 1 }}>
                  <Input.Password placeholder="secret" name="service_password"
                    autoComplete="new-password"
                    data-lpignore="true" data-1p-ignore="true" data-bwignore="true" />
                </Form.Item>
              </Space.Compact>

              {/* ---- HTTPS ---- */}
              {/* Label + toggle on ONE line, no bottom margin (was a vertical
                  label row + switch row + 24px gap). */}
              <Form.Item style={{ marginBottom: 0 }}>
                <Space size={8} align="center">
                  {/* G41/G47 — the switch gates the instance's TLS vhost AND its
                      stream confs (which are not https at all), so it is named for
                      TLS, the same word the request, CLI and template use. */}
                  <Text>Enable TLS</Text>
                  <Form.Item name="https" valuePropName="checked" noStyle>
                    <Switch onChange={(v) => {
                      setHttpsEnabled(v)
                      if (!v) {
                        setSelectedSslDomain('')
                        form.setFieldsValue({ ssl_domain: undefined, fullchain: '', privkey: '' })
                      } else {
                        // T8/R9 — the Domain follows the certificate, never a
                        // typed string: clear it until a certificate is chosen.
                        setSelectedSslDomain('')
                        form.setFieldsValue({
                          ssl_domain: undefined, fullchain: '', privkey: '', domain: '',
                        })
                      }
                    }} />
                  </Form.Item>
                </Space>
              </Form.Item>
              {httpsEnabled && (
                <>
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
                  {/* R9 — no certificates: TLS stays on, the certificate select is
                      required, the deploy is blocked, and the panel says how to
                      unblock it. */}
                  {sslDomains.length === 0 && (
                    <Alert
                      type="warning"
                      showIcon
                      style={{ marginTop: 8, marginBottom: 0 }}
                      message="No certificates uploaded — upload a certificate first"
                      description={
                        <span>
                          The Domain follows the selected certificate, so it cannot be
                          typed while TLS is on. The deploy is blocked until a
                          certificate is selected. <Button type="link" size="small"
                            style={{ padding: 0 }}
                            onClick={() => navigate('/ssl')}>Open the SSL page</Button>
                        </span>
                      }
                    />
                  )}
                </>
              )}
            </>
          )}

          {/* ---- Dependencies (own section, never folded) ---- */}
          {selectedServiceName && (
            <>
              <Divider plain>Dependencies (services this one calls)</Divider>
              <Form.Item name="dependency_service_ids" style={{ marginBottom: 8 }}>
                <Select
                  mode="multiple"
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  placeholder="None"
                  options={crossTargets}
                />
              </Form.Item>
              {depDuplicate && (
                <Alert type="warning" showIcon style={{ marginBottom: 8 }}
                  message="The same service is selected more than once — the dependencies are removed from the env context"
                  description="They are applied as s2s grants only. The env file and reference pickers behave normally, and no dependency rows are listed." />
              )}
              <div style={{ fontSize: 12, color: '#999' }}>
                The services this deploy depends on. Only accounts this user can
                reach are listed (their own, an active account grant, or a
                public account). Each selected dependency gets an ordinary grant
                row before the app starts; a private target also needs the
                active account grant. The env-generation model uses the
                selection to classify which env vars point at which service.
              </div>
            </>
          )}

          {/* ---- Base file selection (design §Selection & UI) ---- */}
          {/* Not until a source project is chosen: there is no recipe to list
              candidates from, and an empty picker under every open panel read
              as if files were missing before the operator had selected anything. */}
          {selectedServiceName && (
            <>
              <Divider plain>Base file selection <span style={{fontSize:12,color:'#999'}}>— stored default pre-selected; save happens on deploy</span></Divider>
              <FileSetSelection
                selection={selection}
                candidates={fileSetCandidates}
                stale={fileSetStale}
                needsEnv={needsEnv}
                onChange={setSelection}
                onOpenFile={(name) => openSelectedFile(name)}
                forceEnvGenerate={false}
              />
              {!!depKey && eff.env_none && (
                <Alert type="info" showIcon style={{ marginTop: 8 }}
                  message="None — no env is sent"
                  description="The dependencies are applied as s2s grants only: no classification, no generation, no dependency rows." />
              )}
              {/* R26–R29 — the classification picker. Under the env `None` it
                  is the disabled `None`; otherwise it offers `None`, `generate
                  new` and the fingerprint-matched variants. A red variant is
                  selectable for inspection/editing; Generate/Deploy
                  regenerates it. */}
              <div style={{ marginTop: 10 }}>
                <div style={{ fontSize: 13, marginBottom: 4 }}>Env classification</div>
                {/* The selected variant is listed above the picker like the
                    base-file pickers: click it to inspect/edit, × to drop the
                    explicit choice and return to the default. */}
                {clsIsExisting && (
                  <SelectedFileRow
                    name={effectiveClassification}
                    onOpen={(n) => openSelectedFile(n, false)}
                    onRemove={() => setClassificationChoice('')}
                    stale={!!clsSelectedVariant?.red}
                  />
                )}
                <Select
                  style={{ width: '100%' }}
                  value={effectiveClassification}
                  disabled={envIsNone}
                  onChange={(v) => setClassificationChoice(String(v))}
                  options={envIsNone
                    ? [{ value: 'none', label: 'None' }]
                    : [
                        { value: 'none', label: 'None — no classification' },
                        { value: 'generate_new', label: 'generate new' },
                        ...clsVariants.map((v) => ({
                          value: v.value,
                          title: v.value,
                          label: (
                            <span>
                              {v.exact && !v.red && (
                                <CheckCircleTwoTone twoToneColor="#52c41a"
                                  style={{ marginRight: 6 }} />
                              )}
                              {v.value}
                              {v.red && (
                                <WarningOutlined
                                  style={{ color: '#cf1322', marginLeft: 6 }}
                                  title="invalid — will be regenerated" />
                              )}
                            </span>
                          ),
                        })),
                      ]}
                />
                <div style={{ fontSize: 12, color: '#999' }}>
                  {envIsNone
                    ? 'No env is sent, so no classification exists.'
                    : effectiveClassification === 'none'
                      ? 'The selected env is sent as-is; no classification injection happens.'
                      : eff.env.length > 0
                        ? 'The selected env is the root template; the classification is the overwrite plan. Missing env keys make the variant red; Generate/Deploy regenerates it.'
                        : 'A generated classification drives the per-user .env.raw. Every variant is listed; the exact match is marked and a non-matching one is still selectable.'}
                </div>
                {clsCoverageMissing.length > 0 && (
                  <Alert type="error" showIcon style={{ marginTop: 8 }}
                    message="This classification cannot be used"
                    description={`According to the classification, ${clsCoverageMissing.join(', ')} ${clsCoverageMissing.length === 1 ? 'is' : 'are'} missing. The env classification will be regenerated because the currently selected one is invalid.`} />
                )}
                {clsTargetViolations.length > 0 && (
                  <Alert type="error" showIcon style={{ marginTop: 8 }}
                    message="This classification cannot be used"
                    description={`According to the classification, ${clsTargetViolations.map(v => `${v.variable} should point to "${v.target}"`).join('; ')}, but ${clsTargetViolations.length === 1 ? 'that service is' : 'those services are'} not selected as a dependency. The env classification will be regenerated because the currently selected one is invalid.`} />
                )}
                {clsInternalViolations.length > 0 && (
                  <Alert type="error" showIcon style={{ marginTop: 8 }}
                    message="This classification cannot be used"
                    description={`According to the classification, ${clsInternalViolations.map(v => `${v.variable} should point to "${v.target}"`).join('; ')}, but ${clsInternalViolations.length === 1 ? 'that service is' : 'those services are'} inside the same compose — an internal service is never a dependency. The env classification will be regenerated because the currently selected one is invalid.`} />
                )}
                {overwritePreview.length > 0 && (
                  <Alert type="info" showIcon style={{ marginTop: 8 }}
                    message={`Overwrite preview: ${overwritePreview.join(', ')}`}
                    description="These variables are replaced in the injected .env.raw. The selected env file itself is not modified and is not sent. Secret and external values are not printed." />
                )}
              </div>
              {/* R28 — one relation picker per selected target. `cls None`
                  fixes `None`, `cls generate new` fixes `generate new`, both
                  disabled; `cls existing` enables the 1:1 check. */}
              {relationStates.length > 0 && (
                <div style={{ marginTop: 10 }}>
                  <div style={{ fontSize: 13, marginBottom: 4 }}>Dependency relations</div>
                  {relationStates.map((r) => {
                    const disabled = !clsIsExisting
                    const mode = disabled
                      ? (effectiveClassification === 'generate_new' ? 'generate_new' : 'none')
                      : relationModeFor(r.target)
                    // The classification state pins the picker: `cls None` is
                    // exactly `None`, `cls generate new` exactly `generate
                    // new`, both disabled (R28).
                    const opts = disabled
                      ? [{ value: mode, label: mode === 'generate_new' ? 'generate new' : 'None' }]
                      : [
                          ...(r.allowed_none ? [{ value: 'none', label: 'None' }] : []),
                          { value: 'generate_new', label: 'generate new' },
                          ...(r.options || []).map((o: any) => ({
                            value: o.value,
                            title: o.value,
                            label: (
                              <span>
                                {o.exact && !o.red && (
                                  <CheckCircleTwoTone twoToneColor="#52c41a"
                                    style={{ marginRight: 6 }} />
                                )}
                                {o.value}
                                {o.red && (
                                  <WarningOutlined
                                    style={{ color: '#cf1322', marginLeft: 6 }}
                                    title="invalid — will be regenerated" />
                                )}
                              </span>
                            ),
                          })),
                        ]
                    const chosen = (r.options || []).find((o: any) => o.value === mode)
                    return (
                      <div key={r.target} style={{ marginBottom: 8 }}>
                        <div style={{ fontSize: 12, marginBottom: 3 }}>{r.target}
                          {r.projection?.length ? ` — 1:1: ${r.projection.join(', ')}` : ' — empty projection'}
                        </div>
                        {clsIsExisting && String(mode).startsWith('dependency.') && (
                          <SelectedFileRow
                            name={mode}
                            onOpen={(n) => openSelectedFile(n, false)}
                            onRemove={() => setRelationChoices(prev => ({
                              ...prev, [r.target]: 'generate_new' }))}
                            stale={!!chosen?.red}
                          />
                        )}
                        <Select
                          style={{ width: '100%' }}
                          value={mode}
                          disabled={disabled}
                          onChange={(v) => setRelationChoices(prev => ({
                            ...prev, [r.target]: String(v) }))}
                          options={opts}
                        />
                        {chosen?.red && (
                          <Alert type="warning" showIcon style={{ marginTop: 6 }}
                            message="This relation cannot be used"
                            description="According to the classification, this relation lists the wrong variables for the target. The dependency relation will be regenerated because the currently selected one is invalid." />
                        )}
                        {clsIsExisting && !r.allowed_none && (
                          <div style={{ fontSize: 12, color: '#999' }}>
                            `None` is disabled: the classification assigns variables to this target.
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
              {/* ---- Deploy-time per-user env (pipeline env phase, design §2) ---- */}
              {perUserEnv && (
                <Alert type="success" showIcon style={{ marginTop: 8 }} message={`Per-user env ready: ${perUserEnv} — this deployment will use it as its interpolation env`} />
              )}
              {/* S13 — the LLM env guard is part of the env selection: it is
                  enabled only while a selected env file exists (not under
                  `generate new`, not under `None`). It adds a check, never
                  replacing the mechanical ones. */}
              <div style={{ marginTop: 8 }}>
                <Checkbox
                  checked={llmActive && llmValidateEnv}
                  disabled={!llmActive || eff.env.length === 0 || !!eff.env_none}
                  onChange={(e) => setLlmValidateEnv(e.target.checked)}
                >
                  Run LLM validation before deploy
                </Checkbox>
                <div style={{ fontSize: 12, color: '#999' }}>
                  {!llmActive
                    ? 'No LLM configured — LLM validation is unavailable.'
                    : eff.env_none
                    ? 'No env files are sent, so there is nothing to validate.'
                    : eff.env.length === 0
                    ? 'Available when a selected env file is chosen; under generate new the generation path guards the .raw.'
                    : 'Validates the selected env list before deploy; a finding blocks the deploy until it is fixed. Mechanical validation always runs.'}
                </div>
              </div>
            </>
          )}

          {/* ---- Missing Files / LLM Generation ---- */}
          {/* T9/R10 — the prompt + env reference render inside this Alert's
              description, together with the Generate button. */}
          {/* Missing-files / generation UI exists only once a source project is
              chosen. Before that the slots are empty for the trivial reason that
              there is no recipe yet, so announcing "Missing essential files:
              compose, nginx config, env files" and offering a generation prompt
              is noise about a state the operator has not entered. */}
          {!selectedServiceName ? null : checkingMissing ? (
            <div style={{padding:'8px 0'}}><Spin size="small" /> <span style={{fontSize:12,color:'#999'}}>Checking deployment readiness...</span></div>
          ) : checkError ? (
            <Alert type="error"
              message={`Deployment readiness check failed: ${checkError}`}
              description={renderGenerationPrompt()}
              style={{marginBottom:12}} />
          ) : !slotsSpecified ? (
            <Alert
              type={Object.keys(generatedFiles).length > 0 ? 'success' : 'warning'}
              message={
                Object.keys(generatedFiles).length === 0
                  ? `Missing essential files: ${missingLabels.join(', ')}`
                  : undefined
              }
              description={
                <div style={{marginTop:4}}>
                  {renderGenerationPrompt()}
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
                            {/* R3-F48 — coerced, not trusted: an API error body
                                is an OBJECT, and React refuses to render one
                                (#31), which blanks this whole panel — the pause
                                dialog included. */}
                            <Text type="danger" style={{fontSize:12}}>
                              {typeof genJob.jobState.error === 'string'
                                ? genJob.jobState.error
                                : (genJob.jobState.error?.detail || genJob.jobState.error?.error
                                   || `Job ${genJob.jobState.status}`)}
                            </Text>
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
                                onClick={() => { void openSelectedFile(fn, false) }}>
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
          ) : (
            <Alert type="success"
              message="All necessary files are specified, ready for deployment"
              style={{marginBottom:12}} />
          )}

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

          {/* ---- Additional params (optional deploy inputs) ---- */}
          {/* Hidden until a source project is chosen: both lists are derived
              from that recipe's compose and mean nothing without one. */}
          {selectedServiceName && (
            <>
              <Divider plain>Additional params</Divider>
              <Collapse
                size="small"
                ghost
                // Opening the section mounts its Form.List — fill the rows now,
                // not only when the preview response arrived.
                onChange={(keys) => {
                  if ((keys as string[]).includes('volumes')) setTimeout(applyVolumeRows, 0)
                }}
                items={[
                  {
                    key: 'volumes',
                    label: 'Volume Mapping (optional)',
                    // The Form.List must exist BEFORE the auto-fill runs: a
                    // collapsed Collapse does not mount its children, and the fill
                    // effect then set values on a list that had no fields — the
                    // section opened empty (measured 2026-10-24).
                    forceRender: true,
                    children: (
                      !volumePreviewed ? (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          Select a compose file to preview the mount table.
                        </Text>
                      ) : (
                      <Form.List name="volumes">
                        {(fields, { add }) => {
                          volumeAddRef.current = add
                          // A real mount TABLE: sub-service | host path : container
                          // path. The key is the deploy's override-map entry and is
                          // kept hidden; the operator edits only the host path.
                          const COLS = '120px 1fr 16px 140px'
                          return (
                            <>
                              <div style={{ display: 'grid', gridTemplateColumns: COLS, gap: 8, marginBottom: 4 }}>
                                <Text type="secondary" style={{ fontSize: 12 }}>sub-service</Text>
                                <Text type="secondary" style={{ fontSize: 12 }}>host path</Text>
                                <span />
                                <Text type="secondary" style={{ fontSize: 12 }}>container path</Text>
                              </div>
                              {fields.map(({ key, name, ...rest }) => (
                                <div key={key} style={{ display: 'grid', gridTemplateColumns: COLS, gap: 8, alignItems: 'center', marginBottom: 8 }}>
                                  <Form.Item {...rest} name={[name, 'key']} hidden><Input /></Form.Item>
                                  <Form.Item noStyle shouldUpdate>
                                    {() => {
                                      const rows = form.getFieldValue('volumes') || []
                                      return (
                                        <Text code style={{ fontFamily: 'monospace' }}>
                                          {(rows[name] || {}).service || '—'}
                                        </Text>
                                      )
                                    }}
                                  </Form.Item>
                                  <Form.Item {...rest} name={[name, 'value']} rules={[{ required: true, message: 'Host path' }]} style={{ marginBottom: 0 }}>
                                    <Input placeholder="/srv/provision/user-data/alice/app" />
                                  </Form.Item>
                                  <span style={{ textAlign: 'center' }}>:</span>
                                  <Form.Item noStyle shouldUpdate>
                                    {() => {
                                      const rows = form.getFieldValue('volumes') || []
                                      return (
                                        <Text code style={{ fontFamily: 'monospace' }}>
                                          {(rows[name] || {}).target || '—'}
                                        </Text>
                                      )
                                    }}
                                  </Form.Item>
                                </div>
                              ))}
                            </>
                          )
                        }}
                      </Form.List>
                      )
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
            </>
          )}

          {/* ---- Global Proxy (below Additional params) ---- */}
          <Form.Item name="use_global_proxy" valuePropName="checked">
            <Checkbox disabled={!proxyEnabled} onChange={() => { proxyTouchedRef.current = true }}>
              <GlobalOutlined /> Use global proxy for this deployment
              {!proxyEnabled && <span style={{ color: '#999', fontSize: 12 }}> (enable in Settings first)</span>}
              {proxyEnabled && <span style={{ color: '#52c41a', fontSize: 12 }}> (proxy is enabled)</span>}
            </Checkbox>
          </Form.Item>

          <Space style={{ justifyContent: 'flex-end', width: '100%' }}>
            {fileSetLoading && (
              <Text type="secondary" style={{ fontSize: 12 }}><Spin size="small" /> Loading file selection…</Text>
            )}
            {/* G57 — profiles are OPTIONAL: no `--profile` is the compose CLI
                default and a complete, legitimate deployment. Say which
                services stay off, as a neutral note; never imply a profile is
                required. */}
            {!fileSetLoading && (fileSetCandidates.profiles || []).length > 0 && effectiveSelection().profiles.length === 0 && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                No optional profile selected — services gated behind{' '}
                {(fileSetCandidates.profiles || []).join(', ')} will not start
                (the compose CLI default).
              </Text>
            )}
            {/* F4/S14 — say WHY Deploy is disabled while TLS has no certificate. */}
            {httpsOn && !watchedSslDomain && (
              <Text type="warning" style={{ fontSize: 12 }}>
                Select an SSL certificate to enable Deploy.
              </Text>
            )}
            {/* Debug export: the selected options plus the generation path's
                output, opened as raw JSON text in a new browser tab. */}
            <Button htmlType="button" icon={<DownloadOutlined />} onClick={exportGenerationJson}>
              Export JSON
            </Button>
            <Button type="primary" htmlType="submit" loading={loading} icon={<span>🚀</span>}
              // `computingLabel` gates the submit too: the next-label call resolves through the
              // -api's status computation and was measured taking ~3–6s, during which the
              // (read-only) Label field still shows the PREVIOUS value. Submitting in that window
              // sends the stale label — e.g. 0 for a pair that already has 0 — and the register
              // then fails with "already registered", which is the collision the label work exists
              // to prevent. The field already shows a spinner while computing; the button must
              // agree with it.
              // An empty compose slot is NOT a reason to disable Deploy: the
              // gateway's generation path produces the compose (as it does for
              // any other empty slot), so Deploy is how the operator asks for it.
              disabled={!selectedUserName || !selectedServiceName || !!checkError || fileSetLoading
                || computingLabel
                // R26 — a classification pointing at an unselected target is
                // regenerated on Generate/Deploy when an LLM is active; with no
                // LLM there is no way to repair it, so the deploy is blocked
                // with the same named reason.
                || (clsTargetViolations.length > 0 && !llmActive)
                // F4/S14 — TLS on with no certificate selected: no trusted
                // domain, so Deploy is visibly disabled. Read from the FORM
                // watcher, not the stale `httpsEnabled` state.
                || (httpsOn && !watchedSslDomain)}>
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
          {/* Informational only: an empty compose slot does not block Deploy,
              because the pipeline generates one. Say which empty-compose state
              this is (stale files vs. nothing stored) so the operator knows
              whether picking a file is a repair or just a preference. Suppressed
              in fixing mode, where the pre-filled guide is the real signal. */}
          {!loading && selectedUserName && selectedServiceName && !fileSetLoading && !computingLabel && !fixingGuide
            && (fileSetCandidates.compose || []).length > 0 && effectiveSelection().compose.length === 0 && (
            <Alert type="info" showIcon style={{ marginTop: 8 }}
              message="No compose file is selected for this deployment"
              // An empty compose slot does not block Deploy: the gateway's
              // generation path creates the compose. The two causes still read
              // differently, because the remedy (a pick vs. a new generation)
              // differs — but neither one is a reason the button is disabled.
              description={
                ((fileSetStale || {}).compose || []).length > 0
                  ? 'The stored selection names files that no longer exist (they may have been converted to .j2 templates or removed). Deploy will generate a new compose with the pipeline; pick a file above to use an existing one instead.'
                  : 'This recipe has no stored selection yet. Deploy will generate a compose with the pipeline; pick a file above to use an existing one instead.'
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
      // B2 — a closed popup leaves NO DOM behind. Without this the previous
      // leg's "Paused — waiting for you" title and credential form stayed
      // mounted (display:none) after the panel closed, and a fresh panel had a
      // hidden pause modal in the document.
      destroyOnClose
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
