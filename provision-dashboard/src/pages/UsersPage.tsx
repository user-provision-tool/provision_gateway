import { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Typography, Card, Tag, Space, Button, Empty, Spin, message, Input, Collapse, Badge, Tooltip, Modal, Drawer, Checkbox, Popconfirm } from 'antd'
import { RocketOutlined, ReloadOutlined, EyeOutlined, EyeInvisibleOutlined, SearchOutlined, CaretRightOutlined, PauseOutlined, DeleteOutlined, CopyOutlined, LinkOutlined, KeyOutlined, UnlockOutlined, SwapOutlined, UnorderedListOutlined, DashboardOutlined, GlobalOutlined, StopOutlined, RobotOutlined } from '@ant-design/icons'
import Editor from '@monaco-editor/react'
import { useAuth } from '../hooks/useAuth'
import client from '../api/client'
import DeployForm from '../components/services/DeployForm'
import ServiceCard from '../components/users/ServiceCard'

const { Title, Text } = Typography

// Basename of a server-side path ('' when unknown) — used to turn the -api's
// per-instance file paths into the filenames the per-user-file API expects.
const baseName = (p?: string) => (p ? p.split('/').pop() || '' : '')

interface ServiceInstance {
  user_name: string; service_name: string; label: string;
  // R6.1: the sub-services this instance exposes a STREAM endpoint for, as the
  // canonical names the -api recorded when it rendered the stream conf. Empty or
  // absent means nothing is exposed — which is also the TLS-off case by
  // construction, so the panel needs NO logic of its own for it.
  stream_sub_services?: string[]
  // R1.5/R6.2: the instance's EXTRA http endpoints, as the whole served names
  // (`<instance host>--<sub_service>.<domain>`) the -api read out of the rendered
  // conf. The URL section lists one `/go/` row per entry, keyed by the
  // sub-service, beside the primary's own row keyed `primary` — and offers
  // nothing that the deployment did not create.
  http_endpoints?: string[]
  nginx_conf_paths?: string[]
  healthy_containers?: Record<string,string>
  unhealthy_containers?: Record<string,string>
  missing_containers?: Record<string,string>
  compose_template_path?: string; nginx_conf_template_path?: string
  compose_file_path?: string
  // Recipe subdir of this instance relative to its project dir ('' = root) and
  // the effective per-user env file — supplied by the -api (single source of
  // truth) so the Deployment-Files links resolve inside the right recipe.
  recipe_path?: string
  env_file_path?: string
  has_auth?: boolean; url?: string
  // R1.5 — the instance's own host, recorded in the registry entry and reported
  // by the -api. Every `/go/` link on the card is built from it.
  hostname?: string
  status?: string  // "building" | "running" | "failed" | "unknown"
  // Bounded failure reason, present only on a `failed` entry (GRILLING G10):
  // {stage, message, at}. Instance-scoped state from the registry — the card
  // never reads the task store.
  // `reason` is a STABLE code from the -api (e.g. "at_capacity") — the card
  // branches on it rather than pattern-matching the human-facing message.
  failure?: { stage?: string; message?: string; reason?: string; at?: string }
  // The task this instance came from, whether its log still exists, and whether
  // its record still resolves — all three computed by the -api, which owns the
  // registry and the tasks (llm-based-fix-todo.md §4/§5). `fix_available` false
  // = log gone; `task_available` false = the RECORD is gone.
  task_id?: string
  fix_available?: boolean
  task_available?: boolean
  volumes?: Record<string,string>
}

// The service VALUE the deploy panel expects: `name@@recipe_path`, or the bare
// name for the project root. Both card kinds must seed it this way.
//
// `setPreselectedDeploy(sess.service_name)` alone dropped the recipe, so a
// session on `example-service@@recipes/failbuild` reopened the panel on the
// PROJECT ROOT — and the whole restart then ran against the root recipe: the
// `.fix` compose, the rendered per-user compose and the nginx conf all landed
// at the project root while the recipe dir kept none of them. Found live in
// T13's re-run (2026-09-23): the instance came up on a root-recipe compose
// instead of the fixed one.
const sessionServiceValue = (s: { service_name: string; recipe_path?: string }) =>
  s.recipe_path ? `${s.service_name}@@${s.recipe_path}` : s.service_name

// The card header's status note — ONE LINE, inline after the service name
// (ServiceCard rule 1). The long form rides in the tooltip: the remedy is worth
// keeping, the height it cost is not. `nowrap` is load-bearing — a wrapped note
// grows the header exactly like the block alert it replaces.
function HeaderNote({ tone, text, detail }: {
  tone: 'info' | 'warning' | 'error'; text: string; detail?: string
}) {
  const color = tone === 'error' ? '#cf1322' : tone === 'warning' ? '#d46b08' : '#0958d9'
  return (
    <Tooltip title={detail || undefined}>
      <Text style={{ color, fontSize: 12, whiteSpace: 'nowrap' }}>
        {tone === 'error' ? '✗' : '⚠'} {text}
      </Text>
    </Tooltip>
  )
}

// The pre-deploy card's HEADER note (GRILLING G24 rule 2): whatever the operator
// must know about a session is shown on the header, so it is visible whether the
// card is folded or expanded. A `refused` session explains the refusal and its
// remedy; a `paused` one says what it is waiting for.
function SessionAlert({ sess }: { sess: any }) {
  // G29: the GATEWAY's own pool refused — a different pool from the -api's,
  // with a different remedy ("wait for a generation, or cancel one" versus
  // "cancel a hanging task"). Same header, its own words: the card is the
  // surface the operator actually reads, so an unlabelled `refused` would send
  // them hunting for a docker task that does not exist.
  if (sess.state === 'refused' && sess.reason === 'generation_at_capacity') {
    return (
      <HeaderNote
        tone="warning"
        text="generation pool was full — this generation was refused"
        detail={sess.detail || 'Wait for a running generation to finish, or cancel one, then retry.'}
      />
    )
  }
  if (sess.state === 'refused' && sess.reason === 'at_capacity') {
    return (
      <HeaderNote
        tone="warning"
        text="platform was at capacity — this deploy was refused"
        detail={sess.detail || 'Cancel a task that has been hanging, then retry.'}
      />
    )
  }
  if (sess.state === 'refused') {
    return (
      <HeaderNote
        tone="error"
        text={`refused (${sess.reason || 'unknown'})`}
        detail={sess.detail || 'Retry to replay it, or Delete to remove the record.'}
      />
    )
  }
  if (sess.state === 'paused' && sess.reason === 'llm_fix') {
    return (
      <HeaderNote
        tone="info"
        text="the analysis found the fault — Check to confirm the fix and restart"
        detail="Check opens the deploy panel with the fixing guide pre-filled. Confirming restarts the path, generating .fix files."
      />
    )
  }
  if (sess.state === 'paused') {
    return (
      <HeaderNote
        tone="info"
        text={`paused — ${sess.reason || 'awaiting you'}`}
        detail="Check re-enters the paused path: the deploy panel, and the pause popup that is waiting for your input."
      />
    )
  }
  return null
}

// The failed instance card's HEADER note (GRILLING G24 rule 2). These used to
// sit in the card BODY, where they were invisible whenever the card was folded —
// and the at-capacity one is the whole remedy for a refusal, so hiding it until
// the operator expands a card they have no reason to expand defeated it.
//
// ONE line, at most — it used to stack TWO alerts, because the two conditions
// below fire together: a deploy refused for `at_capacity` holds no task, so its
// log is gone by construction and the second alert always came with the first.
// Folding them into one instruction is therefore the normal case, not a corner.
function FailedAlert({ svc, isAnalyzing }: { svc: ServiceInstance; isAnalyzing: boolean }) {
  if (svc.status !== 'failed') return null
  // §4d (user ruling 2026-09-20): a deploy refused because the -api's task pool
  // was full is retryable once something hanging is cancelled.
  const atCapacity = svc.failure?.reason === 'at_capacity'
  // Log-gone state (§5): the fix is unavailable and there is nothing to link to,
  // so the header says what to do instead. The log is deliberately NOT persisted
  // anywhere else.
  const logGone = !svc.fix_available && !isAnalyzing
  if (!atCapacity && !logGone) return null
  if (atCapacity && logGone) {
    return (
      <HeaderNote
        tone="warning"
        text="platform at capacity and the task log is gone — cancel a hanging task, then remove this service and retry"
        detail="The platform was at capacity, so this deploy was refused; a refused deploy holds no task, so there is no log to feed the LLM fix. Cancel a hanging task, then remove this service and retry."
      />
    )
  }
  if (atCapacity) {
    return (
      <HeaderNote
        tone="warning"
        text="platform was at capacity — cancel a hanging task, then retry"
        detail="Cancel a task that has been hanging, then retry."
      />
    )
  }
  return (
    <HeaderNote
      tone="warning"
      text="task log is gone, cannot fix the deployment files with LLM, just remove this service and retry"
    />
  )
}

export default function UsersPage() {
  const { admin } = useAuth()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const isAdmin = admin?.role === 'admin'
  const [services, setServices] = useState<ServiceInstance[]>([])
  const [loading, setLoading] = useState(true)
  const deployParam = searchParams.get('deploy') || ''
  const [deployOpen, setDeployOpen] = useState(!!deployParam)
  const [preselectedDeploy, setPreselectedDeploy] = useState(deployParam || undefined)
  const [preselectedUser, setPreselectedUser] = useState<string | undefined>(undefined)
  // Retry pins the failed entry's label (§5.1/§5.2).
  const [preselectedLabel, setPreselectedLabel] = useState<string | undefined>(undefined)
  // Fix ANALYSES in flight (GRILLING G26). Computed gateway-side and read from
  // /pre-deploy: while one runs the instance is still `failed` and no session
  // exists yet, so neither store alone can express `failed + analyzing`.
  const [analyses, setAnalyses] = useState<any[]>([])
  // `check` on a `paused` session re-enters the paused path IN PLACE: the deploy
  // panel opens attached to the parked job (so its pause popup is there) or, for
  // an LLM-fix pause, pre-filled with the analysis's guide.
  const [resumeJobId, setResumeJobId] = useState<number|null>(null)
  const [fixingGuide, setFixingGuide] = useState<{guide:string; slot:string}|null>(null)
  // §8b pre-deploy sessions: work that is NOT an instance yet, so it is stored
  // by the GATEWAY, not the -api registry — a session rendered from the instance
  // store would look registered. Closing the deploy panel keeps the session
  // alive (explicitly not `close = cancel`); these cards are the way back.
  const [sessions, setSessions] = useState<any[]>([])
  // A REFUSED deployment's stored options, handed to the deploy panel so the
  // operator confirms exactly what will be replayed (GRILLING G20).
  const [retryOptions, setRetryOptions] = useState<Record<string, any> | null>(null)
  // The minted exposure URL, held ONLY here and only for this page's lifetime:
  // the API returns it once and can never return it again (R2.2), so this is the
  // single place it survives — and it dies with the tab, which is the point.
  const [exposureUrls, setExposureUrls] = useState<Record<string,string>>({})
  const [exposureModal, setExposureModal] = useState<{url:string; port:number; row:string} | null>(null)

  // R6.1/R6.2: mint from the SUB-SERVICE alone. The port is not sent — the
  // registry records names, never ports, so the gateway resolves the row from the
  // instance's own serving_ports artifact. The URL comes back ONCE.
  const mintExposure = async (svc: ServiceInstance, canonical: string) => {
    const sub = canonical.includes('--')
      ? canonical.split('--').slice(1).join('--').split('.')[0]
      : canonical.split('.')[0]
    try {
      const { data } = await client.post('/stream/exposures', {
        service_name: svc.service_name, user_name: svc.user_name,
        label: String(svc.label), sub_service: sub,
      })
      // `data.address` — the NAME and the stream PORT, and nothing else.
      //
      // The port is the half that must not be lost: `data.host` alone is what an
      // operator pastes into a client, and a client given no port dials its
      // protocol default (7687 for Bolt) — the instance's INTERNAL port, which
      // answers nothing — so the row looked broken when the platform was fine.
      // Measured 2026-10-16.
      //
      // No scheme: the platform does not name the stream protocol (user ruling
      // 2026-10-16). Everything at or above the door is protocol-blind, and the
      // client supplies its own protocol's spelling.
      setExposureUrls(prev => ({...prev, [canonical]: data.address}))
      setExposureModal({url: data.address, port: data.stream_port, row: sub})
    } catch (e: any) {
      message.error(e?.response?.data?.detail || 'Could not generate an exposure URL')
    }
  }
  // G29: for a GATEWAY-side refusal the retry is NOT a replay. The generation
  // pool refused before anything was produced, so the retry has to run the
  // generation again — the panel seeds itself from the record, and the pipeline
  // must still run. (The -api's `at_capacity` refusal is the opposite: its
  // options are the paths that were SENT, so the replay must not regenerate.)
  const [retryRegenerate, setRetryRegenerate] = useState(false)
  // G20 for the `check` restart: the paused session's own proxy setting, so the
  // deploy panel opens with the tick the path was created with and the restart
  // replays it instead of registering `build_args: {}`. Mirrors how the label is
  // carried — it is the session's own value, not a fresh default.
  const [preselectedUseGlobalProxy, setPreselectedUseGlobalProxy] = useState<boolean | null>(null)
  // `.fix` artifacts found in the card's own RECIPE, keyed `service@@recipe` —
  // shown alongside the deployed files so a fix is visible as PRESENT without
  // opening the deploy panel (llm-based-fix-todo.md §6). They are already
  // selectable in the panel's pickers, since the candidate scans match them.
  //
  // SCOPED TO THE RECIPE, not the service: these artifacts are recipe-level by
  // design (GRILLING G33 — compose and nginx are user-agnostic names in the
  // recipe dir; only env is per-user), so per-(user, label) isolation is not the
  // right unit, but asking per SERVICE made a card list another recipe's fix
  // file — a card on `recipes/conflict` showed `recipes/interp`'s.
  const [fixFiles, setFixFiles] = useState<Record<string, string[]>>({})

  // Load ONE recipe's `.fix` candidates (cached per service@@recipe).
  const loadFixFiles = async (service: string, recipePath: string) => {
    const cacheKey = recipePath ? `${service}@@${recipePath}` : service
    if (!service || fixFiles[cacheKey]) return
    try {
      const { data } = await client.get(`/projects/${service}/file-sets`)
      // `.` is the root recipe's key, as everywhere else this payload is read.
      const cands = (data.candidates || {})[recipePath || '.'] || {}
      const found = new Set<string>()
      for (const kind of ['compose', 'nginx', 'env']) {
        for (const n of ((cands as any)[kind] || [])) {
          if (typeof n === 'string' && /\.fix(\.|$)/.test(n)) found.add(n)
        }
      }
      setFixFiles(prev => ({ ...prev, [cacheKey]: Array.from(found).sort() }))
    } catch { /* candidates are a nicety — never block the card */ }
  }
  const [search, setSearch] = useState('')
  const [visiblePwds, setVisiblePwds] = useState<Record<string,boolean>>({})

  // Password change modal
  const [pwdModalOpen, setPwdModalOpen] = useState(false)
  const [pwdTarget, setPwdTarget] = useState<{user:string;service:string;label:string}|null>(null)
  const [newPassword, setNewPassword] = useState('')
  const [pwdLoading, setPwdLoading] = useState(false)

  // Clone modal
  const [cloneOpen, setCloneOpen] = useState(false)
  const [cloneSource, setCloneSource] = useState<string | null>(null)
  const [cloneTarget, setCloneTarget] = useState('')
  const [cloneLoading, setCloneLoading] = useState(false)

  // Deployment file editor drawer
  const [editorOpen, setEditorOpen] = useState(false)
  const [editorFile, setEditorFile] = useState<{user:string;service:string;label:string;fileType:string;filename:string;path:string;recipePath:string}|null>(null)
  const [editorContent, setEditorContent] = useState('')
  const [editorOriginal, setEditorOriginal] = useState('')
  const [delOpen, setDelOpen] = useState(false)
  const [delTarget, setDelTarget] = useState<{user_name:string;service_name:string;label:string}|null>(null)
  const [delToken, setDelToken] = useState('')
  const [delInput, setDelInput] = useState('')
  // Batch (multi-select) delete also uses the typed-confirm modal (feature 2 —
  // no plain-Popconfirm delete path remains).
  const [delBatch, setDelBatch] = useState(false)
  // Optimistic "Deleting…" overlay (the durable state is the -api registry
  // status 'deleting'; this only bridges click → next poll).
  const [deletingKeys, setDeletingKeys] = useState<Set<string>>(new Set())
  const [editorLoading, setEditorLoading] = useState(false)
  const [editorSaving, setEditorSaving] = useState(false)

  // Registration times cache: key → unix timestamp
  const [regTimes, setRegTimes] = useState<Record<string, number|null>>({})
  // File modification times cache: key → unix timestamp
  const [fileModTimes, setFileModTimes] = useState<Record<string, number|null>>({})
  // Track which services need redeploy (files modified after registration)
  const [needsRedeploy, setNeedsRedeploy] = useState<Record<string, boolean>>({})

  // Volume usage cache: key → {volumes: {...}, user_data_dir: string}
  const [volumeUsage, setVolumeUsage] = useState<Record<string, any>>({})

  // Container resource stats cache: key → {cpu_percent, mem_usage_mb, mem_total_mb, disk_usage_mb}
  const [resourceStats, setResourceStats] = useState<Record<string, {cpu: string, mem: string, disk: string}>>({})

  // Batch selection state
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set())
  const [batchLoading, setBatchLoading] = useState(false)

  // Rebuild confirmation modal
  const [rebuildTarget, setRebuildTarget] = useState<{user:string;service:string;label:string;noCache:boolean}|null>(null)
  const [rebuildProxy, setRebuildProxy] = useState(false)
  const [rebuildLoading, setRebuildLoading] = useState(false)

  // Track which panels are currently expanded (for container-status refresh).
  // Sessions and instances now share ONE `Collapse` per user, so they share this
  // set too: a session's panel key is namespaced (`session:…`) against an
  // instance's (`user-service-label`), so the two cannot collide — and the
  // separate array the split used to need is gone with the split.
  const [expandedPanels, setExpandedPanels] = useState<string[]>([])
  // Timer ref for container-status refresh
  const refreshTimersRef = useRef<Record<string, ReturnType<typeof setInterval>>>({})
  const servicesRef = useRef(services)
  servicesRef.current = services

  // Build the service-access redirect URL (Gap 9). Clicking it goes to
  // `/go/{hostname}` on the dashboard domain, which the gateway turns into a
  // 302 → `{hostname}/_set_token?token=…` (setting the provision_token cookie for
  // the service domain) → service loads silently. This is required because the
  // login cookie is host-scoped and is NOT sent to the service hostname directly.
  // The instance's OWN host, taken from the URL the -api reports — its domain is
  // whatever the deployment used, and the `/go/` relay is on the dashboard's
  // domain, so only the host part is needed. The previous form hard-coded
  // `.localhost`, so on any other domain every `/go/` link pointed at a host that
  // does not exist.
  const instanceHost = useCallback((svc: ServiceInstance): string => {
    // The RECORDED host first — the registry entry carries it, and it is the
    // single source of truth the deploy itself used.
    if (svc.hostname) return svc.hostname
    try {
      const h = new URL(svc.url || '').hostname
      if (h) return h
    } catch { /* no url yet — fall through to the composed name */ }
    return `${svc.service_name}-${svc.user_name}-${svc.label}.localhost`
  }, [])

  const buildServiceUrl = useCallback((svc: ServiceInstance): string => {
    return `/go/${instanceHost(svc)}`
  }, [instanceHost])

  // R1.5/R6.2 — one `/go/` row per EXTRA http endpoint. The recorded name IS the
  // host to dial (`<instance host>--<sub_service>.<domain>`), and the key shown
  // is the sub-service: the same shape the stream rows use, so one convention
  // covers both.
  const goUrlFor = useCallback((svc: ServiceInstance, servedName: string): string => {
    return `/go/${servedName}`
  }, [])

  // Parse "user-service-label" key (service may contain hyphens like "example-mcp")
  const parseServiceKey = (key: string) => {
    const parts = key.split('-')
    return { user: parts[0], service: parts.slice(1, -1).join('-'), label: parts[parts.length - 1] }
  }

  // Container-status refresh: when a panel is expanded, re-fetch services every 5s
  // so container tags stay up-to-date without full page reload
  useEffect(() => {
    const timers = refreshTimersRef.current
    for (const k of Object.keys(timers)) {
      if (!expandedPanels.includes(k)) { clearInterval(timers[k]); delete timers[k] }
    }
    for (const key of expandedPanels) {
      // A session's panel key is namespaced and belongs to no instance — it has
      // no fix files of its own to show.
      if (!key.startsWith('session:')) {
        const { user, service, label } = parseServiceKey(key)
        const svc = servicesRef.current.find(s =>
          s.user_name === user && s.service_name === service && String(s.label) === String(label))
        loadFixFiles(service, svc?.recipe_path || '')
      }
      if (timers[key]) continue
      timers[key] = setInterval(() => {
        refreshServices()
      }, 5000)
    }
    return () => { for (const t of Object.values(refreshTimersRef.current)) clearInterval(t); refreshTimersRef.current = {} }
  }, [expandedPanels])

  useEffect(() => { loadServices(); loadSessions() }, [])

  useEffect(() => {
    if (services.length === 0) return
    // registration-time is an admin-only endpoint; skip for viewers (Gap 5/10
    // follow-up — otherwise every card emits a 403 in the console).
    if (!isAdmin) return
    // Check registration times for each service
    const checkTimes = async () => {
      for (const svc of services) {
        const key = `${svc.user_name}-${svc.service_name}-${svc.label}`
        if (regTimes[key] !== undefined) continue // Already fetched
        try {
          const { data } = await client.get(`/services/${svc.user_name}/${svc.service_name}/${svc.label}/registration-time`)
          const rt = data.registration_time || null
          setRegTimes(prev => ({...prev, [key]: rt}))
        } catch { /* ignore */ }
      }
    }
    checkTimes()
  }, [services, isAdmin])

  // Fetch volume disk usage for a service instance
  const fetchVolumeUsage = async (user: string, service: string, label: string) => {
    const key = `${user}-${service}-${label}`
    if (volumeUsage[key] !== undefined) return
    try {
      const { data } = await client.get(`/services/${user}/${service}/${label}/volume-usage`)
      setVolumeUsage(prev => ({...prev, [key]: data}))
      // Also populate disk info into resource stats
      if (data.volumes) {
        const volumes = data.volumes
        const totalBytes = Object.values(volumes).reduce((sum: number, v: any) => sum + (v.size_bytes || 0), 0)
        const diskStr = totalBytes > 1073741824 ? (totalBytes / 1073741824).toFixed(1) + 'GB' : totalBytes > 1048576 ? (totalBytes / 1048576).toFixed(0) + 'MB' : (totalBytes / 1024).toFixed(0) + 'KB'
        setResourceStats(prev => ({...prev, [key]: {...(prev[key] || {cpu:'',mem:''}), disk: totalBytes > 0 ? diskStr : ''}}))
      }
    } catch { /* ignore */ }
  }

  // Check file modification times for per-user deployment files
  // (recipe-scoped — the convention list endpoint was retired, GAP-18; the
  // recipe subdir comes from the -api so sub-recipe services like dify resolve).
  const checkFileModTimes = useCallback(async (
    user: string, service: string, label: string,
    recipePath = '', envFile = '', composeFile = '',
  ) => {
    const key = `${user}-${service}-${label}`
    try {
      const composeName = composeFile || `docker-compose.user-${user}.${label}.yml`
      // Probe the env file ONLY when the registry records one — the retired
      // `.env.raw.{user}.{label}` fallback probed (and named) a file that is not
      // a deployment artifact, so it always reported "not there".
      const probes = [
        client.get(`/services/${user}/${service}/${label}/per-user-file`, { params: { recipe_path: recipePath, filename: composeName } }),
      ]
      if (envFile) {
        probes.push(client.get(`/services/${user}/${service}/${label}/per-user-file`, { params: { recipe_path: recipePath, filename: envFile } }))
      }
      const responses = await Promise.all(probes)
      let latestMod: number | null = null
      for (const data of responses.map(r => r.data)) {
        if (data.exists && data.modified_at) {
          if (latestMod === null || data.modified_at > latestMod) {
            latestMod = data.modified_at
          }
        }
      }
      setFileModTimes(prev => ({...prev, [key]: latestMod}))
      // Check if files were modified after registration
      const regTime = regTimes[key]
      if (regTime && latestMod && latestMod > regTime) {
        setNeedsRedeploy(prev => ({...prev, [key]: true}))
      } else {
        setNeedsRedeploy(prev => ({...prev, [key]: false}))
      }
    } catch { /* ignore */ }
  }, [regTimes])

  // Fetch docker stats for container resource usage
  const fetchResourceStats = useCallback(async () => {
    if (services.length === 0) return
    try {
      const { data: statsData } = await client.get('/system/stats?detail=true')
      const containers = statsData.containers || []
      const newStats: Record<string, {cpu: string, mem: string, disk: string}> = {}

      for (const svc of services) {
        const key = `${svc.user_name}-${svc.service_name}-${svc.label}`
        const prefix = `${svc.service_name}-user_${svc.user_name}-${svc.label}-`
        const svcContainers = containers.filter((c: any) => c.name && c.name.startsWith(prefix))

        if (svcContainers.length > 0) {
          const totalCpu = svcContainers.reduce((sum: number, c: any) => {
            const cpuStr = c.cpu_percent || c.cpu || '0'
            return sum + parseFloat(String(cpuStr).replace('%', ''))
          }, 0)
          const totalMemMb = svcContainers.reduce((sum: number, c: any) => {
            const memStr = c.mem_usage_mb || (c.mem_usage || '0')
            return sum + parseFloat(String(memStr))
          }, 0)
          newStats[key] = {
            cpu: isNaN(totalCpu) ? '' : totalCpu.toFixed(1) + '%',
            mem: isNaN(totalMemMb) ? '' : (totalMemMb > 1024 ? (totalMemMb / 1024).toFixed(1) + 'GB' : totalMemMb.toFixed(0) + 'MB'),
            disk: '' // Will be filled from volume usage
          }
        }
      }
      setResourceStats(prev => ({...prev, ...newStats}))
    } catch { /* ignore */ }
  }, [services])

  // When services load, fetch resource stats and check file mod times
  useEffect(() => {
    for (const svc of services) {
      const key = `${svc.user_name}-${svc.service_name}-${svc.label}`
      // per-user-file mod-time checks (checkFileModTimes) are admin-only — skip for viewers
      if (isAdmin && fileModTimes[key] === undefined) {
        checkFileModTimes(
          svc.user_name, svc.service_name, svc.label,
          svc.recipe_path || '', baseName(svc.env_file_path), baseName(svc.compose_file_path),
        )
      }
      if (isAdmin && volumeUsage[key] === undefined) {
        fetchVolumeUsage(svc.user_name, svc.service_name, svc.label)
      }
    }
    if (isAdmin && services.length > 0) {
      fetchResourceStats()
      // Poll resource stats every 15s for live CPU/RAM updates
      const interval = setInterval(fetchResourceStats, 15000)
      return () => clearInterval(interval)
    }
  }, [services, checkFileModTimes])

  // Open deployment file editor
  const openFileEditor = async (user: string, service: string, label: string, fileType: string, filename: string, recipePath = '') => {
    setEditorLoading(true)
    setEditorFile({ user, service, label, fileType, filename, path: '', recipePath })
    setEditorOpen(true)
    try {
      const { data } = await client.get(`/services/${user}/${service}/${label}/per-user-file`, { params: { recipe_path: recipePath, filename } })
      setEditorContent(data.content || '')
      setEditorOriginal(data.content || '')
      setEditorFile(prev => prev ? {...prev, path: data.path || ''} : null)
      if (data.source_fallback) {
        message.info('Loaded from source template. Save to create the per-user deployment file.')
      } else if (!data.exists) {
        message.info('File does not exist yet. Create it by saving.')
      }
    } catch (err: any) {
      if (err.response?.status === 404) {
        setEditorContent('')
        setEditorOriginal('')
        message.info('File does not exist yet. Create it by saving.')
      } else {
        message.error('Failed to load file')
        setEditorOpen(false)
      }
    } finally {
      setEditorLoading(false)
    }
  }

  // Save deployment file
  const saveFile = async () => {
    if (!editorFile) return
    setEditorSaving(true)
    try {
      // Save into the SAME recipe dir the file was read from (never the project
      // root) — otherwise a save would create a stray root-level file.
      await client.put(`/services/${editorFile.user}/${editorFile.service}/${editorFile.label}/per-user-file`, { recipe_path: editorFile.recipePath || '', filename: editorFile.filename, content: editorContent })
      message.success('File saved')
      setEditorOriginal(editorContent)
      // Re-check modification times
      checkFileModTimes(
        editorFile.user, editorFile.service, editorFile.label, editorFile.recipePath,
        editorFile.fileType === 'env' ? editorFile.filename : '',
        editorFile.fileType === 'compose' ? editorFile.filename : '',
      )
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to save')
    } finally {
      setEditorSaving(false)
    }
  }

  // Get language for Monaco based on file type
  const getEditorLanguage = (fileType: string) => {
    switch (fileType) {
      case 'compose': return 'yaml'
      case 'nginx': return 'nginx'
      case 'env': return 'shell'
      default: return 'plaintext'
    }
  }

  // Toggle a single service selection
  const toggleSelect = (key: string) => {
    setSelectedKeys(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key); else next.add(key)
      return next
    })
  }

  // Toggle select all for a user group
  const toggleSelectAll = (userName: string) => {
    const userKeys = services.filter(s => s.user_name === userName).map(s => `${s.user_name}-${s.service_name}-${s.label}`)
    const allSelected = userKeys.every(k => selectedKeys.has(k))
    setSelectedKeys(prev => {
      const next = new Set(prev)
      if (allSelected) { userKeys.forEach(k => next.delete(k)) }
      else { userKeys.forEach(k => next.add(k)) }
      return next
    })
  }

  // Batch action: start/stop/rebuild/remove selected services
  const batchAction = async (action: string) => {
    setBatchLoading(true)
    const keys = Array.from(selectedKeys)
    try {
      for (const key of keys) {
        const {user, service, label} = parseServiceKey(key)
        switch (action) {
          case 'stop': await client.post(`/services/${user}/${service}/${label}/down`); break
          case 'start': await client.post(`/services/${user}/${service}/${label}/up`); break
          case 'rebuild': await client.post(`/services/${user}/${service}/${label}/rebuild`, {no_cache: true}); break
        }
      }
      message.success(`Batch ${action}: ${keys.length} service(s) processed`)
      setSelectedKeys(new Set())
      refreshServices()
    } catch (err: any) {
      message.error(err.response?.data?.detail || `Batch ${action} failed`)
    } finally {
      setBatchLoading(false)
    }
  }

  const _fetchServices = async (): Promise<ServiceInstance[]> => {
    const { data } = await client.get('/services')
    // The backend (GET /api/users, gateway `list_users`) already ACL-filters the
    // response to a viewer's own services + their allowed_special_users. Do NOT
    // re-filter client-side here: the prior `if (endUserViewer && u.user_name !==
    // admin.email) continue` dropped granted special-user services, hiding them
    // from the viewer despite the backend returning them (Gap 3).
    const users = data.users || data.user_status || []
    const all: ServiceInstance[] = []
    for (const u of users) {
      for (const s of (u.healthy_services||[])) all.push({...s, user_name: u.user_name})
      for (const s of (u.unhealthy_services||[])) all.push({...s, user_name: u.user_name})
      for (const s of (u.missing_services||[])) all.push({...s, user_name: u.user_name})
      // Failed entries are their own bucket, classified from the registry
      // status (never container health) — see failed-instance-state-todo.md §3.
      for (const s of (u.failed_services||[])) all.push({...s, user_name: u.user_name})
    }
    return all
  }

  // Drop optimistic "Deleting…" flags for instances that are gone from the
  // server list (so a failed/never-arriving removal can't stick forever).
  const pruneDeleting = (list: ServiceInstance[]) => {
    setDeletingKeys(prev => {
      if (prev.size === 0) return prev
      const live = new Set(list.map(s => `${s.user_name}-${s.service_name}-${s.label}`))
      const next = new Set([...prev].filter(k => live.has(k)))
      return next.size === prev.size ? prev : next
    })
  }

  const loadSessions = async () => {
    try {
      const { data } = await client.get('/pre-deploy')
      setSessions(data.sessions || [])
      setAnalyses(data.analyses || [])
    } catch { /* sessions are additive — never block the page */ }
  }

  const loadServices = async () => {
    setLoading(true)
    try {
      const list = await _fetchServices()
      setServices(list); pruneDeleting(list)
    } catch (err: any) { message.error('Failed to load services') }
    finally { setLoading(false) }
  }

  // Silent refresh — no loading spinner, for auto-polling
  const refreshServices = useCallback(async () => {
    try {
      const list = await _fetchServices()
      setServices(list); pruneDeleting(list)
    } catch { /* silent */ }
  }, [])

  // Auto-poll every 10s — silent refresh, no loading spinner. Sessions and
  // analyses poll with it: `failed + analyzing` is a LIVE gateway-side state, and
  // a card that only learns about it on mount would show it never.
  useEffect(() => {
    const interval = setInterval(() => { refreshServices(); loadSessions() }, 10000)
    return () => clearInterval(interval)
  }, [refreshServices])

  // ONE grouping, TWO card kinds. A pre-deploy session is NOT a separate
  // category: it is the same service card at an earlier point of the path, so it
  // renders inside its own user's group, next to that user's instances. A user
  // with only sessions gets a group of their own instead of a second section —
  // which is also why the `Empty` state no longer has to sit beside a list of
  // sessions on the same page.
  const groups = useMemo(() => {
    const q = search.toLowerCase()
    const matches = (user: string, service: string) =>
      !q || user.toLowerCase().includes(q) || service.toLowerCase().includes(q)
    const order: string[] = []
    const svcsByUser: Record<string, ServiceInstance[]> = {}
    const sessByUser: Record<string, any[]> = {}
    const ensure = (un: string) => {
      if (!svcsByUser[un]) { svcsByUser[un] = []; sessByUser[un] = []; order.push(un) }
    }
    for (const s of services) {
      const un = s.user_name || 'unknown'
      ensure(un)
      if (matches(un, s.service_name)) svcsByUser[un].push(s)
    }
    for (const sess of sessions) {
      const un = sess.user_name || 'unknown'
      ensure(un)
      if (matches(un, sess.service_name)) sessByUser[un].push(sess)
    }
    return order
      .map(un => ({ userName: un, svcs: svcsByUser[un], sessions: sessByUser[un] }))
      .filter(g => g.svcs.length > 0 || g.sessions.length > 0)
  }, [services, sessions, search])

  const getBadge = (s: ServiceInstance, key?: string, analyzing = false) => {
    // Failed: its own state, read from the registry — never an ordinary
    // "0 up, 1 down", which is what a failed entry used to look like.
    // `+ analyzing` (GRILLING G26 row 2) is layered on from the GATEWAY's own
    // analysis job while one runs: the instance is still `failed` in the -api
    // registry at that moment, because nothing moves until the analysis
    // succeeds, so the registry alone cannot express this state.
    if (s.status === 'failed') {
      const stage = s.failure?.stage ? ` (${s.failure.stage})` : ''
      if (analyzing) return <Badge status="processing" text={`Failed${stage} + analyzing`}/>
      return <Badge status="error" text={`Failed${stage}`}/>
    }
    // Building: containers don't exist yet, don't show misleading "down" count
    if (s.status === 'building') return <Badge status="processing" text="Building..."/>
    // Deleting: persisted server-side in the registry (like 'building'), so it
    // survives a page refresh; the local key set only bridges the instant
    // between the click and the next poll.
    if (s.status === 'deleting' || (key && deletingKeys.has(key))) {
      return <Badge status="processing" text="Deleting..."/>
    }
    const h = Object.keys(s.healthy_containers||{}).length
    const uh = Object.keys(s.unhealthy_containers||{}).length
    const m = Object.keys(s.missing_containers||{}).length
    if (!h&&!uh&&!m) return <Badge status="default" text="unknown"/>
    if (!uh&&!m) return <Badge status="success" text={`Running (${h})`}/>
    return <Badge status="warning" text={`${h} up, ${uh+m} down`}/>
  }

  // Cancel a `building` service by cancelling the -api task behind it (§4b).
  // The task list carries each task's {user_name, service_name, label} target,
  // so the card finds its own task rather than the registry carrying a task id.
  const cancelBuilding = async (svc: ServiceInstance) => {
    try {
      const { data } = await client.get('/tasks')
      const tasks = data.tasks || []
      const task = tasks.find((t: any) =>
        (t.status === 'running' || t.status === 'pending') &&
        t.target?.user_name === svc.user_name &&
        t.target?.service_name === svc.service_name &&
        String(t.target?.label ?? '') === String(svc.label)
      )
      if (!task) {
        message.warning('No running task found for this service — refreshing')
        refreshServices()
        return
      }
      await client.delete(`/tasks/${task.task_id}`)
      message.success('Cancellation requested — the service will be marked failed')
      refreshServices()
    } catch (e: any) {
      message.error(e.response?.data?.detail || 'Cancel failed')
    }
  }

  const openPwdChange = (user:string, service:string, label:string) => {
    setPwdTarget({user,service,label})
    setNewPassword('')
    setPwdModalOpen(true)
  }

  const handlePwdChange = async () => {
    if (!pwdTarget || !newPassword) { message.warning('Enter a new password'); return }
    setPwdLoading(true)
    try {
      await client.put(`/services/${pwdTarget.user}/${pwdTarget.service}/${pwdTarget.label}/password`, { passwd: newPassword })
      message.success('Password changed')
      setPwdModalOpen(false)
    } catch (err: any) { message.error(err.response?.data?.detail || 'Failed') }
    finally { setPwdLoading(false) }
  }

  // Drop the service's own auth_basic layer. The card keeps its ACL gate.
  // Needed for a protected instance whose registry plaintext is gone: the gate
  // can never satisfy that auth_basic again, so the layer has to come off.
  const handleRemovePwd = async (user: string, service: string, label: string) => {
    try {
      await client.delete(`/services/${user}/${service}/${label}/password`)
      message.success('Service password removed — auth_basic is off, the ACL gate still applies')
      refreshServices()
    } catch (err: any) { message.error(err.response?.data?.detail || 'Failed to remove the service password') }
  }

  const handleCloneAll = async () => {
    if (!cloneSource || !cloneTarget) { message.warning('Enter target user'); return }
    setCloneLoading(true)
    try {
      await client.post('/services/clone', { source_user: cloneSource, target_user: cloneTarget })
      message.success(`Cloning ${cloneSource} → ${cloneTarget}`)
      setCloneOpen(false)
      refreshServices()
    } catch (err: any) { message.error(err.response?.data?.detail || 'Failed') }
    finally { setCloneLoading(false) }
  }

  const handleRebuildConfirm = async () => {
    if (!rebuildTarget) return
    const { user, service, label, noCache } = rebuildTarget
    const key = `${user}-${service}-${label}`
    setRebuildLoading(true)
    // Optimistic: mark as building immediately
    setServices(prev => prev.map(s => s.user_name === user && s.service_name === service && s.label === label ? {...s, healthy_containers:{}, unhealthy_containers:{}, missing_containers:{}, status:'building'} : s))
    try {
      const payload: any = {}
      if (noCache) payload.no_cache = true
      if (rebuildProxy) payload.use_global_proxy = true
      const r = await client.post(`/services/${user}/${service}/${label}/rebuild`, payload)
      const taskId = r.data?.task_id || r.data?.id
      message.success({content:<span>{noCache ? 'Redeploying' : 'Rebuilding'}... {taskId && <Button type="link" size="small" icon={<UnorderedListOutlined/>} onClick={()=>navigate('/tasks')}>View Task</Button>}</span>,duration:5})
      if (noCache) setNeedsRedeploy(prev => ({...prev, [key]: false}))
    } catch(e:any) { message.error(e.response?.data?.detail||'Failed') }
    setRebuildTarget(null)
    setRebuildProxy(false)
    setRebuildLoading(false)
    setTimeout(refreshServices, 2000)
  }

  // Highlight matching text
  const highlight = (text: string, query: string) => {
    if (!query) return <span>{text}</span>
    const idx = text.toLowerCase().indexOf(query.toLowerCase())
    if (idx === -1) return <span>{text}</span>
    return <span>{text.substring(0,idx)}<mark style={{background:'#ffd666',padding:'0 2px'}}>{text.substring(idx,idx+query.length)}</mark>{text.substring(idx+query.length)}</span>
  }

  // The pre-deploy SESSION card — the same widget as an instance (GRILLING G24),
  // differing only by status, rendered INSIDE its own user's group rather than in
  // a section of its own. The session's two job-live/stopped states drive which
  // actions exist — `cancel` belongs to a live job, `check`/`retry` to a stopped
  // one, and they never appear together.
  const renderSessionCard = (sess: any) => {

    const key = `session:${sess.user_name}-${sess.service_name}-${sess.label}`
    // CANCEL FOLLOWS THE JOB, not the card's label (G24 rule 4: "the
    // cancel button is for canceling a job … when the job stopped the
    // button should go away"). `job_live` is computed by the GATEWAY
    // from the job's real status — a `job_id` alone cannot tell
    // "parked, cancellable" from "finished an hour ago".
    // (Corrected 2026-09-26: this used to add "and a paused session's
    // job is genuinely live (parked in `awaiting_input`)" — true before
    // G27, which made a pause TERMINAL. `job_live` deliberately
    // excludes `paused`, which is exactly why a paused card offers
    // `Check`/`Delete` and no `Cancel`.)
    // Keying only on `generating` left a dead end: a paused card
    // offered `delete`, the server answered "cancel it first", and no
    // cancel existed. Found live in T14.
    const isJobLive = !!sess.job_live
    return (
      <ServiceCard
        key={key}
        panelKey={key}
        // The BADGE follows the session's own state; only the ACTIONS
        // follow the job. A `paused` session whose job is parked for
        // input is waiting on the OPERATOR — calling that "Generating…"
        // (because the job is technically live) hides the one thing the
        // card exists to say. Caught by T14 after `job_live` was
        // introduced for the cancel gate.
        badge={
          <Badge
            status={sess.state === 'generating' ? 'processing'
                    : sess.state === 'refused' ? 'error'
                    : sess.state === 'generated' ? 'success' : 'warning'}
            text={sess.state === 'generating' ? 'Generating…'
                  : sess.state === 'refused' ? `Refused (${sess.reason || 'unknown'})`
                  // The leg ENDED and its files are ready; the deploy has not been
                  // sent. Without this case the state fell into the `else` branch
                  // and the card read "Paused (awaiting you)" — which was a lie
                  // about a path that is simply waiting for the operator to press
                  // Deploy.
                  : sess.state === 'generated' ? 'Ready to deploy'
                  : `Paused (${sess.reason || 'awaiting you'})`}
          />
        }
        title={<Text strong>{sess.service_name}</Text>}
        label={<Tag>{sess.label}</Tag>}
        headerExtra={<Text type="secondary">{sess.user_name}</Text>}
        alert={<SessionAlert sess={sess} />}
        actions={<>
          {/* SESSION-ACTIONS-START — the unit test that guards "no
              instance-only button on a pre-deploy card" reads this
              region; keep the markers. */}
          {/* retry — ONLY for a refused deployment. It replays the
              EXACT stored options straight to the -api: no re-selection
              and no generation pipeline, so files the rejected attempt
              produced are reused as-is. */}
          {sess.state === 'refused' && (
            <Button size="small" type="primary"
              disabled={!sess.options && sess.reason !== 'generation_at_capacity'}
              onClick={() => {
                const genRefusal = sess.reason === 'generation_at_capacity'
                setRetryOptions(sess.options || null)
                // A gateway refusal re-runs the deploy, generation
                // included (G29) — see `retryRegenerate`. Its record
                // may predate the fix and carry no options at all, so
                // the button cannot depend on them: the session's own
                // fields (user, service, label, proxy) are enough to
                // open the panel on the same deployment.
                setRetryRegenerate(genRefusal)
                setPreselectedDeploy(sessionServiceValue(sess))
                setPreselectedUser(sess.user_name)
                setPreselectedLabel(String(sess.label))
                // The refused options carry `use_global_proxy`
                // themselves (they are replayed verbatim), so the
                // session-level seed must not also fire — except for a
                // generation refusal with no stored options, where the
                // session column is the only source.
                setPreselectedUseGlobalProxy(
                  genRefusal && !sess.options ? !!sess.use_global_proxy : null)
                setDeployOpen(true)
              }}>Retry</Button>
          )}
          {/* check — re-enter the PATH (G15): the deploy pane as
              if never closed, PLUS the pause popup. For a generation
              pause that is the panel attached to the parked job; for an
              LLM-fix pause it is the panel pre-filled with the guide.
              A `generated` record offers it too: the leg is over and its
              files are ready, so re-opening the panel attached to that
              leg is what lets the operator press Deploy on the handoff
              instead of re-running the whole generation. */}
          {(sess.state === 'paused' || sess.state === 'generated') && (
            <Button size="small" type="primary" onClick={() => {
              setPreselectedDeploy(sessionServiceValue(sess))
              setPreselectedUser(sess.user_name)
              setPreselectedLabel(String(sess.label))
              // The session's proxy setting rides along with the
              // label and the guide (G20): the restart is the SAME
              // path, so it replays the options it was created with.
              setPreselectedUseGlobalProxy(!!sess.use_global_proxy)
              // The GUIDE only belongs to a pause that is still waiting. A
              // `generated` record keeps the reason it was paused with, so
              // matching on `reason` alone would replay a fix that has
              // already run — and open the panel in fixing mode.
              if (sess.state === 'paused' && sess.reason === 'llm_fix') {
                setFixingGuide({ guide: sess.guide || '', slot: sess.slot || 'compose' })
              } else if (sess.job_id != null) {
                setResumeJobId(sess.job_id)
              }
              setDeployOpen(true)
            }}>Check</Button>
          )}
          {/* cancel — a JOB action, so it exists only while a job is
              live. It used to render for every state and report
              success unconditionally, so on a refused session (which
              has no job by construction) it did nothing and claimed it
              had. */}
          {isJobLive && (
            <Button size="small" danger onClick={async () => {
              try {
                if (sess.job_id == null) {
                  message.warning('This session has no running job to cancel')
                  return
                }
                const r = await client.delete(`/llm/jobs/${sess.job_id}`)
                // The endpoint answers 200 for ANY existing job: it
                // no-ops when the job has already stopped and still
                // returns the row. Reporting success off the response
                // alone is the same lie the `refused` session used to
                // tell — this session's state is a MIRROR of the job,
                // refreshed only by the periodic reconcile, so it can
                // still read `generating` after the job failed. Read
                // the status the server actually reports.
                if (r.data?.status === 'cancelled') {
                  message.success('Session cancelled')
                } else {
                  message.warning(
                    `Nothing to cancel — the job is already ${r.data?.status || 'stopped'}`)
                }
                loadSessions()
              } catch (e: any) {
                const d = e.response?.data?.detail
                message.error(typeof d === 'string' ? d : (d?.detail || 'Cancel failed'))
              }
            }}>Cancel</Button>
          )}
          {/* delete — removes the RECORD, and **implicitly cancels a
              live job first** (user ruling 2026-09-23: "the delete
              implicitly forcing a cancel first"). It used to be hidden
              while a job was live, which made it depend on a second
              button; deleting IS the operator saying "stop this and
              forget it", so it is always offered and does the cancel
              itself. */}
          <Button size="small" danger onClick={async () => {
            try {
              const { data } = await client.delete(`/pre-deploy/${sess.user_name}/${sess.service_name}/${sess.label}`, {
                params: { recipe_path: sess.recipe_path || '' },
              })
              message.success(data?.cancelled_job
                ? 'Running job cancelled and session removed'
                : 'Session removed')
              loadSessions()
            } catch (e: any) {
              const d = e.response?.data?.detail
              message.error(typeof d === 'string' ? d : (d?.detail || 'Delete failed'))
            }
          }}>Delete</Button>
          {/* SESSION-ACTIONS-END */}
        </>}
      >
        <Space direction="vertical" style={{ width: '100%' }} size="small">
          <div>
            <Text strong>Recorded paths (what the -api sees): </Text>
            {sess.options
              ? <Space wrap>
                  <Tag color={sess.options.compose_file_paths?.length ? 'green' : 'red'}>
                    compose: {sess.options.compose_file_paths?.join(', ') || 'none'}
                  </Tag>
                  {/* G55 — one conf per category. The record's map is the
                      authority; the legacy singular is shown for older rows. */}
                  <Tag color={sess.options.nginx_confs?.http || sess.options.nginx_conf_file_path ? 'green' : 'red'}>
                    nginx: {(sess.options.nginx_confs?.http || sess.options.nginx_conf_file_path) || 'none'}
                  </Tag>
                  <Tag color={sess.options.nginx_confs?.stream ? 'green' : 'red'}>
                    stream: {sess.options.nginx_confs?.stream || 'none'}
                  </Tag>
                  <Tag color={sess.options.env_files?.length ? 'green' : 'red'}>
                    env: {sess.options.env_files?.join(', ') || 'none'}
                  </Tag>
                </Space>
              : <Text type="secondary">none recorded yet</Text>}
          </div>
          {sess.job_id != null && (
            <div>
              <Text strong>Job: </Text>
              {/* The Tasks page reads `?job=` and shows THIS gateway
                  generation job's own step log. The link used to go to
                  `/tasks?job=<id>` where nothing read the parameter,
                  and the id is a GATEWAY job id — not one of the -api
                  tasks that page lists — so it could not have resolved
                  even if it had. */}
              <Button type="link" size="small" style={{ padding: 0 }}
                onClick={() => navigate(`/tasks?job=${sess.job_id}`)}>#{sess.job_id}</Button>
            </div>
          )}
          {sess.task_id && (
            <div>
              <Text strong>Analysed the failed task: </Text>
              {/* A real -api task with a real log — link it, so the
                  evidence the analysis read is one click away. */}
              <Button type="link" size="small" style={{ padding: 0 }}
                onClick={() => navigate(`/tasks?task=${sess.task_id}`)}>
                <Text code>{sess.task_id}</Text>
              </Button>
            </div>
          )}
          {sess.guide && (
            <div>
              <Text strong>Fixing guide from the analysis: </Text>
              <Text code style={{ whiteSpace: 'pre-wrap', display: 'block', marginTop: 4 }}>{sess.guide}</Text>
            </div>
          )}
        </Space>
      </ServiceCard>
    )
  }

  return (
    <div>
      <div style={{display:'flex',justifyContent:'space-between',marginBottom:16,flexWrap:'wrap',gap:8}}>
        <Title level={3} style={{margin:0}}>Services</Title>
        <Space>
          {/* B1 / G58 — this is NOT a credential field, and it sits on a page
              that also renders password inputs (the deploy panel). A bare
              unannotated text input is exactly what a browser's password
              manager treats as the username target, so opening the panel filled
              it with the saved login and the filter matched nothing. Opt out by
              shape (search, not text) and by name — never by name that looks
              like a login. */}
          <Input prefix={<SearchOutlined/>} placeholder="Filter..." type="search"
            id="services-filter" name="services_filter" autoComplete="off"
            value={search} onChange={e=>setSearch(e.target.value)} allowClear style={{width:240}}/>
          {isAdmin && <Button type="primary" icon={<RocketOutlined/>} onClick={()=>setDeployOpen(true)}>Deploy</Button>}
          <Button icon={<ReloadOutlined/>} onClick={loadServices}>Refresh</Button>
        </Space>
      </div>

      {selectedKeys.size > 0 && isAdmin && (
        <Card size="small" style={{marginBottom:12,background:'#fffbe6',borderColor:'#faad14'}}>
          <Space>
            <Text strong>{selectedKeys.size} selected</Text>
            <Button size="small" loading={batchLoading} onClick={()=>batchAction('stop')}>Stop</Button>
            <Button size="small" loading={batchLoading} onClick={()=>batchAction('start')}>Start</Button>
            <Button size="small" loading={batchLoading} icon={<RocketOutlined/>} onClick={()=>batchAction('rebuild')}>Rebuild</Button>
            <Button size="small" danger loading={batchLoading} disabled={batchLoading} onClick={()=>{
              setDelBatch(true); setDelToken(Math.random().toString(36).slice(2,8)); setDelInput(''); setDelOpen(true)
            }}>Remove</Button>
            <Button size="small" onClick={()=>setSelectedKeys(new Set())}>Clear</Button>
          </Space>
        </Card>
      )}

      {loading ? <Spin/> : groups.length===0 ? (
        <Card><Empty description={search?"No matches":"No services deployed"}/></Card>
      ) : (
        groups.map(({ userName, svcs: userSvcs, sessions: userSessions }, idx) => {
          const isBusy = (s: any) => s.status === 'building' || s.status === 'deleting'
          const allHealthy = userSvcs.every(s => isBusy(s) || (Object.keys(s.unhealthy_containers||{}).length===0 && Object.keys(s.missing_containers||{}).length===0))
          const healthyCount = userSvcs.filter(s => Object.keys(s.healthy_containers||{}).length > 0 && Object.keys(s.unhealthy_containers||{}).length===0 && Object.keys(s.missing_containers||{}).length===0).length
          const buildingCount = userSvcs.filter(s => isBusy(s)).length
          // A failed entry is its own state, not "unhealthy" — it has no
          // containers to be unhealthy about (failed-instance-state-todo.md §3).
          const failedCount = userSvcs.filter(s => s.status === 'failed').length
          const unhealthyCount = userSvcs.length - healthyCount - buildingCount - failedCount
          // The group tag counts INSTANCES (it is a health summary, and a session
          // has no containers to be healthy about); a user holding only sessions
          // says so rather than reading "0 services (all healthy)".
          const groupTag = userSvcs.length === 0
            ? `${userSessions.length} pre-deploy`
            : <>
                {userSvcs.length} service{userSvcs.length>1?'s':''}{
                  failedCount > 0
                    ? ` (${healthyCount} healthy, ${failedCount} failed${unhealthyCount > 0 ? `, ${unhealthyCount} unhealthy` : ''})`
                    : unhealthyCount > 0 ? ` (${healthyCount} healthy, ${unhealthyCount} unhealthy)` : ` (all healthy)`
                }
                {userSessions.length > 0 ? ` + ${userSessions.length} pre-deploy` : ''}
              </>
          return (
          <div key={userName} style={{marginBottom: idx<groups.length-1?32:0, paddingBottom: idx<groups.length-1?16:0, borderBottom: idx<groups.length-1?'1px solid #f0f0f0':'none'}}>
            <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:12}}>
              <Space>
                {isAdmin && <Checkbox
                  checked={userSvcs.length>0 && userSvcs.every(s => selectedKeys.has(`${s.user_name}-${s.service_name}-${s.label}`))}
                  indeterminate={userSvcs.some(s => selectedKeys.has(`${s.user_name}-${s.service_name}-${s.label}`)) && !userSvcs.every(s => selectedKeys.has(`${s.user_name}-${s.service_name}-${s.label}`))}
                  onChange={() => toggleSelectAll(userName)}
                />}
                <Title level={4} style={{margin:0}}>{highlight(userName, search)}</Title>
                <Tag color={failedCount > 0 ? 'red' : (userSvcs.length === 0 ? 'blue' : (allHealthy?'green':'orange'))}>{groupTag}</Tag>
              </Space>
              <Space>
                {isAdmin && <>
                  <Tooltip title="Clone all to another user">
                    <Button size="small" icon={<SwapOutlined/>} onClick={()=>{setCloneSource(userName);setCloneTarget('');setCloneOpen(true)}}>Clone All</Button>
                  </Tooltip>
                  <Button size="small" danger icon={<DeleteOutlined/>} disabled>Del</Button>
                </>}
              </Space>
            </div>

            <Collapse activeKey={expandedPanels} onChange={(keys) => setExpandedPanels(typeof keys === 'string' ? [keys] : keys)}>
              {/* The same widget for both kinds (G24) — the session cards come
                  first, because a session is the instance's own future and the
                  operator's attention is on the work in flight. */}
              {userSessions.map((sess) => renderSessionCard(sess))}
              {userSvcs.map((svc) => {
                const key = `${svc.user_name}-${svc.service_name}-${svc.label}`
                const isDeleting = svc.status === 'deleting' || deletingKeys.has(key)
                const containers = {...svc.healthy_containers, ...svc.unhealthy_containers, ...svc.missing_containers}
                const pwdVisible = visiblePwds[key] || false
                // A fix ANALYSIS running for this instance (GRILLING G26). While
                // one runs the -api entry is deliberately untouched — the
                // instance is still `failed` there — so this card state comes
                // from the gateway's own job, never from the registry.
                const analysis = analyses.find((a: any) =>
                  a.user_name === svc.user_name &&
                  a.service_name === svc.service_name &&
                  String(a.label) === String(svc.label))
                const isAnalyzing = !!analysis
                return (
                  <ServiceCard
                    key={key}
                    panelKey={key}
                    badge={getBadge(svc, key, isAnalyzing)}
                    title={<Text strong>{highlight(svc.service_name, search)}</Text>}
                    label={<Tag>{svc.label}</Tag>}
                    headerExtra={<>
                      {isAdmin && <Checkbox checked={selectedKeys.has(key)} onChange={()=>toggleSelect(key)} onClick={(e:any)=>e.stopPropagation()}/>}
                      {resourceStats[key] && Object.keys(svc.healthy_containers||{}).length > 0 && (
                        <span style={{marginLeft:8,display:'inline-flex',gap:6,alignItems:'center'}}>
                          {resourceStats[key].cpu && <Tag color="blue" style={{fontSize:11,margin:0}}><DashboardOutlined/> CPU {resourceStats[key].cpu}</Tag>}
                          {resourceStats[key].mem && <Tag color="purple" style={{fontSize:11,margin:0}}>RAM {resourceStats[key].mem}</Tag>}
                        </span>
                      )}
                    </>}
                    alert={<FailedAlert svc={svc} isAnalyzing={isAnalyzing} />}
                    actions={isAdmin && <>
                          {/* A `building` entry is cancellable (§4b). Under the hood
                              this cancels the underlying -api task — the same
                              operation the Tasks page button performs; the registry
                              then moves building → failed ("cancelled by operator"). */}
                          {svc.status === 'building' && (
                            <Tooltip title="Cancel this build — the task is cancelled and the service is marked failed">
                              <Button size="small" danger icon={<StopOutlined/>}
                                onClick={()=>cancelBuilding(svc)}>Cancel</Button>
                            </Tooltip>
                          )}
                          {/* An ANALYSIS in flight is cancellable too (G26 row 2):
                              the LLM based fix button is gone while it runs, and
                              cancelling simply returns the instance to `failed` —
                              a clean no-op, because nothing has moved yet. */}
                          {isAnalyzing && (
                            <Tooltip title="Cancel the analysis — the service returns to failed">
                              <Button size="small" danger icon={<StopOutlined/>}
                                onClick={async () => {
                                  try {
                                    await client.delete(`/llm/jobs/${analysis.job_id}`)
                                    message.success('Analysis cancelled')
                                    loadSessions()
                                  } catch (e: any) {
                                    message.error(e.response?.data?.detail || 'Cancel failed')
                                  }
                                }}>Cancel</Button>
                            </Tooltip>
                          )}
                          {/* A `failed` entry's actions belong HERE, not in the
                              body: the body is detail, the header is what the
                              operator acts on. */}
                          {svc.status === 'failed' && !isAnalyzing && <>
                            {svc.fix_available && (
                              <Tooltip title="Analyse the failed task's log with the LLM and write a fixing guide — the deployment is then paused for you to confirm and restart it">
                                <Button size="small" icon={<RobotOutlined/>}
                                  onClick={async () => {
                                    try {
                                      await client.post('/llm/jobs', {
                                        service_name: svc.service_name,
                                        recipe_path: svc.recipe_path || '',
                                        job_type: 'fix',
                                        user_name: svc.user_name,
                                        label: svc.label,
                                      })
                                      message.success('Analysing the failure — the card will pause with the guide when it finishes')
                                      loadSessions()
                                    } catch (e: any) {
                                      const d = e.response?.data?.detail
                                      message.error(typeof d === 'string' ? d : (d?.detail || 'Could not start the analysis'))
                                    }
                                  }}>LLM based fix</Button>
                              </Tooltip>
                            )}
                            <Button size="small" type="primary" icon={<RocketOutlined/>}
                              onClick={()=>{
                                // The recipe rides along: retrying a failed
                                // entry on a recipe subdir must reopen THAT
                                // recipe, not the project root (T13, 2026-09-23).
                                setPreselectedDeploy(sessionServiceValue(svc))
                                setPreselectedUser(svc.user_name)
                                setPreselectedLabel(svc.label)
                                // A failed-entry Retry is not a session resume:
                                // there is no paused session to carry a proxy
                                // from, so the panel keeps its own default.
                                setPreselectedUseGlobalProxy(null)
                                setDeployOpen(true)
                              }}>Retry</Button>
                            <Button size="small" danger icon={<DeleteOutlined/>}
                              onClick={()=>{
                                setDelTarget({user_name:svc.user_name, service_name:svc.service_name, label:svc.label})
                                setDelToken(Math.random().toString(36).slice(2,8))
                                setDelInput(''); setDelOpen(true)
                              }}>Delete</Button>
                          </>}
                          {/* The GENERIC set — stop/start, Rebuild, Redeploy,
                              key, Dup, Delete. It is what a NORMAL instance
                              offers, and it must not run alongside the `failed`
                              set above: both carry a Delete, so a failed card
                              rendered TWO of them (and a Rebuild/Redeploy/Dup
                              it has no use for — Retry is a failed entry's
                              designed recovery, §5.1). Found by trace T13's
                              first look at the card.
                              Actions are disabled while the instance is being
                              deleted (server status 'deleting' or optimistic). */}
                          {svc.status !== 'building' && svc.status !== 'failed' && !isDeleting && <Tooltip title={Object.keys(svc.healthy_containers||{}).length > 0 ? 'Stop containers' : 'Start containers'}>
                            <Button size="small" 
                              icon={Object.keys(svc.healthy_containers||{}).length > 0 ? <PauseOutlined/> : <CaretRightOutlined/>}
                              type={Object.keys(svc.healthy_containers||{}).length > 0 ? 'default' : 'primary'}
                              onClick={()=>{
                                const isRunning = Object.keys(svc.healthy_containers||{}).length > 0
                                const action = isRunning ? 'down' : 'up'
                                const label = isRunning ? 'Stopping...' : 'Starting...'
                                client.post(`/services/${svc.user_name}/${svc.service_name}/${svc.label}/${action}`)
                                  .then(()=>{message.success(label); refreshServices()})
                                  .catch(e=>message.error(e.response?.data?.detail||'Failed'))
                              }}
                            />
                          </Tooltip>}
                          {svc.status !== 'failed' && <>
                          <Button size="small" disabled={isDeleting} onClick={()=>{
                            setRebuildTarget({user:svc.user_name,service:svc.service_name,label:svc.label,noCache:false})
                            setRebuildProxy(false)
                          }}>Rebuild</Button>
                          <Tooltip title="Redeploy service with no-cache rebuild">
                            <Button size="small" icon={<RocketOutlined/>} disabled={isDeleting}
                              className={needsRedeploy[key] ? 'redeploy-blink' : ''}
                              style={needsRedeploy[key] ? {borderColor:'#faad14',color:'#faad14'} : {}}
                              onClick={()=>{
                            setRebuildTarget({user:svc.user_name,service:svc.service_name,label:svc.label,noCache:true})
                            setRebuildProxy(false)
                          }}>Redeploy</Button>
                          </Tooltip>
                          <Tooltip title="Change password">
                            <Button size="small" icon={<KeyOutlined/>} disabled={isDeleting} onClick={()=>openPwdChange(svc.user_name,svc.service_name,svc.label)}/>
                          </Tooltip>
                          <Popconfirm
                            title="Remove this service's password?"
                            description="auth_basic is stripped from the instance; the ACL gate still applies."
                            onConfirm={()=>handleRemovePwd(svc.user_name,svc.service_name,svc.label)}
                          >
                            <Tooltip title="Remove service password">
                              <Button size="small" disabled={isDeleting} icon={<UnlockOutlined/>}/>
                            </Tooltip>
                          </Popconfirm>
                          <Tooltip title="Duplicate for another user">
                            <Button size="small" icon={<CopyOutlined/>} disabled={isDeleting} onClick={()=>{
                              // Duplicate targets the same recipe the card shows.
                              setPreselectedDeploy(sessionServiceValue(svc))
                              setDeployOpen(true)
                            }}>Dup</Button>
                          </Tooltip>
                          <Button size="small" danger icon={<DeleteOutlined/>} disabled={isDeleting} onClick={()=>{
                            setDelTarget({user_name:svc.user_name, service_name:svc.service_name, label:svc.label})
                            setDelToken(Math.random().toString(36).slice(2,8))
                            setDelInput(''); setDelOpen(true)
                          }}/>
                          </>}
                    </>}
                  >
                    <Space direction="vertical" style={{width:'100%'}} size="small">
                      {svc.status === 'failed' && (
                        <div style={{background:'#fff2f0', border:'1px solid #ffccc7', borderRadius:6, padding:'8px 12px'}}>
                          <Space direction="vertical" size={4} style={{width:'100%'}}>
                            <Space>
                              <Text strong style={{color:'#cf1322'}}>Deployment failed</Text>
                              {svc.failure?.stage && <Tag color="red">{svc.failure.stage}</Tag>}
                              {svc.failure?.at && <Text type="secondary" style={{fontSize:12}}>{svc.failure.at}</Text>}
                            </Space>
                            {/* The ALERTS and the ACTIONS live on the card header
                                (GRILLING G24): the header is visible folded, acts
                                as the action surface, and is where the operator
                                looks — the body is detail. What remains here is
                                the failure's own detail: its stage, its time, and
                                the -api's own message. */}
                            {svc.failure?.message && (
                              <Text code style={{whiteSpace:'pre-wrap', wordBreak:'break-all', fontSize:12}}>
                                {svc.failure.message}
                              </Text>
                            )}
                            {isAnalyzing && (
                              <Text type="secondary">
                                The LLM is analysing this failure's log — nothing has been
                                changed yet. Cancel on the card header to go back to failed.
                              </Text>
                            )}
                          </Space>
                        </div>
                      )}
                      <div>
                        <Text strong>URL: </Text>
                        <a href={buildServiceUrl(svc)} target="_blank" rel="noopener noreferrer">
                          <Text code style={{color:'#1677ff'}}>{buildServiceUrl(svc)}</Text>
                        </a>
                        {svc.has_auth && <Tag color="blue" style={{marginLeft:4}}>Auth</Tag>}
                      </div>
                      {/* R6.2 — the URL section: one `/go/` row per exposed HTTP
                          endpoint, keyed by the sub-service name, the primary
                          keyed `primary`. Every row is read from the artifacts
                          (the entry's recorded endpoints), never typed, and the
                          section appears only when the deployment HAS an extra
                          endpoint — an instance with just its primary shows the
                          single URL above, unchanged. */}
                      {(svc.http_endpoints || []).length > 0 && (
                        <div>
                          <Text strong>Endpoints: </Text>
                          <Space direction="vertical" size={2}>
                            <Space size={6} wrap>
                              <Text code>primary</Text>
                              <a href={buildServiceUrl(svc)} target="_blank" rel="noopener noreferrer">
                                <Text code style={{color:'#1677ff'}}>{instanceHost(svc)}</Text>
                              </a>
                            </Space>
                            {(svc.http_endpoints || []).map((served: string) => {
                              const sub = served.includes('--')
                                ? served.split('--').slice(1).join('--').split('.')[0]
                                : served
                              return (
                                <Space key={served} size={6} wrap>
                                  <Text code>{sub}</Text>
                                  <a href={goUrlFor(svc, served)} target="_blank" rel="noopener noreferrer">
                                    <Text code style={{color:'#1677ff'}}>{served}</Text>
                                  </a>
                                </Space>
                              )
                            })}
                          </Space>
                        </div>
                      )}
                      {/* R6.2 — the exposure endpoints, read from the RECORDED
                          state (the entry's stream sub-services). Nothing here
                          re-derives "TLS is on and a row exists": with TLS off
                          nothing was recorded, so nothing is offered. The URL
                          itself is a credential — shown once, kept only in this
                          page's memory, never fetched back. */}
                      {(svc.stream_sub_services || []).length > 0 && (
                        <div>
                          <Text strong>Stream endpoints: </Text>
                          <Space direction="vertical" size={2}>
                            {(svc.stream_sub_services || []).map((canonical: string) => {
                              const sub = canonical.includes('--')
                                ? canonical.split('--').slice(1).join('--').split('.')[0]
                                : canonical
                              const url = exposureUrls[canonical]
                              return (
                                <Space key={canonical} size={6} wrap>
                                  <Text code>{sub}</Text>
                                  {/* Minting is REVOKE + INSERT: the row's previous URL dies at
                                      its next connection. That is the one consequence the
                                      operator has to agree to BEFORE it happens, so it is a
                                      confirmation rather than a warning shown afterwards — the
                                      old modal claimed the URL was shown only once, while the
                                      card kept showing it, and the thing that actually matters
                                      (the old one dying) was buried in the same sentence. */}
                                  <Popconfirm
                                    title="Old urls will be invalidated, confirm?"
                                    description="The previous exposure URL for this row stops working at its next connection."
                                    okText="Generate"
                                    cancelText="Cancel"
                                    onConfirm={() => mintExposure(svc, canonical)}
                                  >
                                    <Button size="small" type="primary" ghost>
                                      Generate exposure URL
                                    </Button>
                                  </Popconfirm>
                                  {url && (
                                    <Text code copyable={{text: url}} style={{fontSize:12}}>{url}</Text>
                                  )}
                                </Space>
                              )
                            })}
                          </Space>
                          <div>
                            <Text type="secondary" style={{fontSize:12}}>
                              The client must speak TLS — dial the name with the shared stream port
                              (shown when you generate the URL). The application itself needs none.
                            </Text>
                          </div>
                        </div>
                      )}
                      {(svc as any).subnet && (
                        <div><Text strong>Subnet: </Text><Tag color="purple">{(svc as any).subnet}</Tag></div>
                      )}
                      {/* The instance's OWN task — the one that last acted on it
                          (the -api stamps it at register and re-stamps it on
                          rebuild). `task_available` is the SERVER's answer to
                          "does that record still resolve?", so the card never
                          probes per instance: clickable while it exists, GREYED
                          once the cleanup contract has taken it (G30). */}
                      {svc.task_id && (
                        <div>
                          <Text strong>Task: </Text>
                          {svc.task_available === true ? (
                            <Button type="link" size="small" style={{padding:0}}
                              onClick={() => navigate(`/tasks?task=${svc.task_id}`)}>
                              <Text code>{svc.task_id}</Text>
                            </Button>
                          ) : (
                            <Tooltip title="the task record is gone — it left by the cleanup contract (TTL / cap)">
                              <Text code type="secondary" style={{cursor:'not-allowed'}}>{svc.task_id}</Text>
                            </Tooltip>
                          )}
                        </div>
                      )}
                      <div><Text strong>Containers: </Text>
                        <Space wrap>{Object.entries(containers).map(([n,s])=>{
                          const status = String(s).toLowerCase();
                          const color = status.includes('up') || status.includes('healthy') ? 'green'
                            : status.includes('unhealthy') ? 'orange'
                            : 'red';
                          return <Tag key={n} color={color}>{n}: {String(s)}</Tag>
                        })}</Space>
                      </div>
                      {isAdmin && <>
                      <div><Text strong>Deployment Files: </Text></div>
                      <div style={{paddingLeft:16}}>
                        {(() => {
                          // The recipe subdir comes from the -api (registry =
                          // single source of truth); '' = project root. Passing
                          // it keeps these links AND the editor's save inside
                          // the recipe dir the deploy actually wrote to (a
                          // hardcoded '' resolved to the project root and made
                          // every file look missing).
                          const recipePath = svc.recipe_path || ''
                          // The env file is whatever the -api recorded at
                          // registration — never a guess. `.env.raw.{user}.{label}`
                          // is the gateway's GENERATION INTERMEDIATE (a bare name
                          // the -api copies to the canonical .env.{n}.{user}.{label}
                          // and then records); it is not a deployment file and is
                          // absent from the registry, so falling back to it printed
                          // a path that never existed and opened an empty editor.
                          // A deployment with no recorded env genuinely has none
                          // (e.g. a compose with no ${VAR} surface) — say so.
                          const envFile = baseName(svc.env_file_path)
                          const composeFile = baseName(svc.compose_file_path) || `docker-compose.user-${svc.user_name}.${svc.label}.yml`
                          // nginx is one of the three deployed paths (G13) and
                          // the -api records its path on the entry — the card
                          // simply never read it. It sits beside the fix list that
                          // already names `nginx.fix.conf`.
                          const nginxFile = baseName(svc.nginx_conf_template_path)
                          const recipeHint = `PROVISION/source_projects/${svc.service_name}${recipePath ? '/' + recipePath : ''}`
                          // ONE line, THIS recipe's own fix artifacts (§9). A
                          // `.fix` is rare and specific, so an empty one renders
                          // nothing at all rather than a `none` note.
                          const recipeFixes = fixFiles[recipePath ? `${svc.service_name}@@${recipePath}` : svc.service_name] || []
                          return <>
                            <div style={{marginBottom:4}}>
                              <Text>.env: </Text>
                              {envFile
                                ? <Text code style={{cursor:'pointer',color:'#1677ff',textDecoration:'underline'}}
                                    onClick={() => openFileEditor(svc.user_name, svc.service_name, svc.label, 'env', envFile, recipePath)}>{envFile}</Text>
                                : <Text type="secondary">none — this deployment records no per-user env file</Text>}
                              {envFile && <Text type="secondary" style={{fontSize:11}}> (in {recipeHint} dir)</Text>}
                            </div>
                            <div style={{marginBottom:4}}>
                              <Text>compose: </Text>
                              <Text code style={{cursor:'pointer',color:'#1677ff',textDecoration:'underline'}}
                                onClick={() => openFileEditor(svc.user_name, svc.service_name, svc.label, 'compose', composeFile, recipePath)}>{composeFile}</Text>
                              <Text type="secondary" style={{fontSize:11}}> (in {recipeHint} dir)</Text>
                            </div>
                            <div style={{marginBottom:4}}>
                              <Text>nginx: </Text>
                              {nginxFile
                                ? <Text code style={{cursor:'pointer',color:'#1677ff',textDecoration:'underline'}}
                                    onClick={() => openFileEditor(svc.user_name, svc.service_name, svc.label, 'nginx', nginxFile, recipePath)}>{nginxFile}</Text>
                                : <Text type="secondary">none — this deployment records no nginx conf</Text>}
                              {nginxFile && <Text type="secondary" style={{fontSize:11}}> (in {recipeHint} dir)</Text>}
                            </div>
                            {recipeFixes.length > 0 && (
                              <div style={{marginBottom:4}}>
                                <Text>LLM fix: </Text>
                                <Space size={4} wrap>
                                  {recipeFixes.map(f => (
                                    <Text key={f} code
                                      style={{cursor:'pointer',color:'#1677ff',textDecoration:'underline',
                                              background:'#f6ffed', borderColor:'#b7eb8f'}}
                                      onClick={() => openFileEditor(
                                        svc.user_name, svc.service_name, svc.label,
                                        // a `.fix` compose opens as compose, a
                                        // `.fix` nginx as nginx
                                        f.endsWith('.conf') ? 'nginx' : (f.startsWith('.env') ? 'env' : 'compose'),
                                        f, recipePath)}>{f}</Text>
                                  ))}
                                </Space>
                                <Text type="secondary" style={{fontSize:11}}> (in {recipeHint} dir)</Text>
                              </div>
                            )}
                          </>
                        })()}
                        {/* ssl */}
                        {svc.url && svc.url.startsWith('https') && (() => {
                          const domain = new URL(svc.url).hostname.split('.').slice(-2).join('.')
                          return <div style={{marginBottom:4}}>
                            <Text>ssl: </Text>
                            <Text code>fullchain.pem, privkey.pem</Text>
                            <Text type="secondary" style={{fontSize:11}}> (in PROVISION/ssl/{domain} dir)</Text>
                          </div>
                        })()}
                      </div>
                      </>}
                      {(svc.volumes && Object.keys(svc.volumes).length > 0) && <div>
                        <Text strong>Volumes: </Text>
                        <div style={{paddingLeft:16}}>
                          {Object.entries(svc.volumes).map(([k,v]) => (
                            <div key={k} style={{marginBottom:2}}>
                              <Tag color="blue">{k}</Tag>
                              <Text type="secondary" style={{fontSize:11,marginLeft:4}}>→ {String(v)}</Text>
                            </div>
                          ))}
                        </div>
                      </div>}
                    </Space>
                  </ServiceCard>
                )
              })}
            </Collapse>
          </div>
        )})
      )}

      <DeployForm
        open={deployOpen}
        preselectedService={preselectedDeploy}
        preselectedUser={preselectedUser}
        preselectedLabel={preselectedLabel}
        preselectedUseGlobalProxy={preselectedUseGlobalProxy}
        initialOptions={retryOptions}
        regenerate={retryRegenerate}
        resumeJobId={resumeJobId}
        fixingGuide={fixingGuide}
        onClose={()=>{setDeployOpen(false); setPreselectedDeploy(undefined); setPreselectedUser(undefined); setPreselectedLabel(undefined); setPreselectedUseGlobalProxy(null); setRetryOptions(null); setRetryRegenerate(false); setResumeJobId(null); setFixingGuide(null); if(deployParam) setSearchParams({})}}
        onDeployed={(taskId: string, user: string, service: string, label: string)=>{
        setDeployOpen(false); setPreselectedDeploy(undefined); if(deployParam) setSearchParams({});
        // Optimistic update: immediately show the new service as "building"
        const optimisticEntry: ServiceInstance = {
          user_name: user, service_name: service, label: label,
          healthy_containers: {}, unhealthy_containers: {}, missing_containers: {},
          status: 'building',
        }
        setServices(prev => {
          // Remove any existing entry for this service (in case of redeploy)
          const filtered = prev.filter(s => !(s.user_name === user && s.service_name === service && s.label === label))
          return [...filtered, optimisticEntry]
        })
        // Then also do a real reload (will update once registry is written)
        setTimeout(refreshServices, 2000)
      }}/>

      {/* Password change modal */}
      <Modal
        title={delBatch ? `Delete ${selectedKeys.size} services — confirm` : 'Delete service — confirm'}
        open={delOpen}
        onCancel={()=>{ setDelOpen(false); setDelBatch(false) }}
        okText="Delete" okButtonProps={{ danger: true, disabled: delInput !== delToken }}
        onOk={() => {
          if (delInput !== delToken) return
          // Capture the targets, then dismiss the popup IMMEDIATELY — the
          // teardown is an async -api task, so we don't wait for the round-trip.
          const keys: string[] = delBatch
            ? Array.from(selectedKeys)
            : (delTarget ? [`${delTarget.user_name}-${delTarget.service_name}-${delTarget.label}`] : [])
          setDelOpen(false); setDelBatch(false); setDelInput('')
          if (keys.length === 0) return
          // Optimistic "Deleting..." until the next poll (the -api also persists
          // a 'deleting' registry status, which is what survives a refresh).
          setDeletingKeys(prev => new Set([...prev, ...keys]))
          if (delBatch) setSelectedKeys(new Set())
          for (const key of keys) {
            const {user, service, label} = parseServiceKey(key)
            client.delete(`/services/${user}/${service}/${label}`)
              .then(() => { message.success(`Deleting ${service}/${label}…`); refreshServices() })
              .catch(() => {
                setDeletingKeys(prev => { const n = new Set(prev); n.delete(key); return n })
                message.error('Failed to delete')
              })
          }
        }}>
        <p>This <b>permanently deletes</b> {delBatch ? 'the selected instances' : 'the instance'}: its containers, generated files <b>and data volumes</b> (decision #2 — nothing is kept). This cannot be undone.</p>
        <p>Type <Text code>{delToken}</Text> to confirm:</p>
        <Input value={delInput} onChange={(e)=>setDelInput(e.target.value)} placeholder="confirm string" />
      </Modal>
      <Modal title="Change Service Password" open={pwdModalOpen} onCancel={()=>setPwdModalOpen(false)}
        onOk={handlePwdChange} confirmLoading={pwdLoading} okText="Change">
        <Space direction="vertical" style={{width:'100%'}} size="middle">
          <div><Text strong>Service: </Text>{pwdTarget?.service}/{pwdTarget?.user}/{pwdTarget?.label}</div>
          <Input.Password prefix={<KeyOutlined/>} placeholder="New password" value={newPassword} onChange={e=>setNewPassword(e.target.value)}/>
        </Space>
      </Modal>

      {/* Rebuild / Redeploy confirmation modal */}
      <Modal
        title={rebuildTarget?.noCache ? 'Redeploy Service' : 'Rebuild Service'}
        open={!!rebuildTarget}
        onCancel={() => { setRebuildTarget(null); setRebuildProxy(false) }}
        onOk={handleRebuildConfirm}
        confirmLoading={rebuildLoading}
        okText={rebuildTarget?.noCache ? 'Redeploy' : 'Rebuild'}
      >
        <Space direction="vertical" style={{width:'100%'}} size="middle">
          <div>
            <Text strong>Service: </Text>
            {rebuildTarget?.service}/{rebuildTarget?.user}/{rebuildTarget?.label}
          </div>
          {rebuildTarget?.noCache && (
            <div>
              <Text type="warning">No-cache rebuild — will rebuild Docker image from scratch.</Text>
            </div>
          )}
          <Checkbox checked={rebuildProxy} onChange={e => setRebuildProxy(e.target.checked)}>
            <Space>
              <GlobalOutlined />
              Use global proxy for this {rebuildTarget?.noCache ? 'redeploy' : 'rebuild'}
            </Space>
          </Checkbox>
        </Space>
      </Modal>

      {/* Clone All modal */}
      <Modal title="Clone All Services" open={cloneOpen} onCancel={()=>setCloneOpen(false)}
        onOk={handleCloneAll} confirmLoading={cloneLoading} okText="Clone">
        <Space direction="vertical" style={{width:'100%'}} size="middle">
          <div><Text strong>Source: </Text><Tag>{cloneSource}</Tag></div>
          <div>
            <Text strong>Target User: </Text>
            <Input placeholder="e.g. bob" value={cloneTarget} onChange={e=>setCloneTarget(e.target.value)}/>
          </div>
          <Text type="secondary">All services from {cloneSource} will be cloned to the target user.</Text>
        </Space>
      </Modal>

      {/* Deployment File Editor Drawer */}
      <Drawer
        title={editorFile ? <Space><Text strong>{editorFile.filename}</Text><Tag>{editorFile.fileType}</Tag><Text type="secondary">for {editorFile.user}/{editorFile.service}/{editorFile.label}</Text></Space> : 'File Editor'}
        open={editorOpen}
        onClose={() => {
          if (editorContent !== editorOriginal) {
            Modal.confirm({
              title: 'Unsaved changes',
              content: 'You have unsaved changes. Discard them?',
              onOk: () => { setEditorOpen(false); setEditorFile(null); }
            })
          } else {
            setEditorOpen(false)
            setEditorFile(null)
          }
        }}
        width="80%"
        extra={
          <Space>
            {editorContent !== editorOriginal && <Tag color="orange">Modified</Tag>}
            <Button onClick={() => setEditorContent(editorOriginal)} disabled={editorContent === editorOriginal}>Reset</Button>
            <Button type="primary" onClick={saveFile} loading={editorSaving} disabled={editorContent === editorOriginal}>
              Save & Close
            </Button>
          </Space>
        }
      >
        {editorLoading ? <Spin /> : (
          <div style={{height:'calc(100vh - 180px)'}}>
            <Editor
              height="100%"
              language={getEditorLanguage(editorFile?.fileType || 'plaintext')}
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
        )}
      </Drawer>
      <Modal
        open={!!exposureModal}
        title="Exposure URL"
        onCancel={() => setExposureModal(null)}
        onOk={() => setExposureModal(null)}
        okText="Close"
        cancelButtonProps={{style:{display:'none'}}}
      >
        {exposureModal && (
          <Space direction="vertical" size={8} style={{width:'100%'}}>
            <Text>
              The client dials this NAME on this PORT with TLS — the name is the credential (it
              travels as the SNI), and the protocol is yours to name: the platform is
              protocol-blind, so add your client's own spelling (<Text code>bolt+s://</Text>,
              <Text code>rediss://</Text>, …). Generating another one for this row invalidates
              this one at its next connection.
            </Text>
            <Text code copyable style={{wordBreak:'break-all'}}>{exposureModal.url}</Text>
            <Text type="secondary" style={{fontSize:12}}>
              Row: {exposureModal.row} · the application behind it needs no TLS of its own.
            </Text>
          </Space>
        )}
      </Modal>
    </div>
  )
}
