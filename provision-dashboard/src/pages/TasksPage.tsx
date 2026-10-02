import { useState, useEffect, useCallback, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Typography, Table, Space, Button, Tag, Drawer, message, Popconfirm } from 'antd'
import { ReloadOutlined, EyeOutlined, PauseCircleOutlined } from '@ant-design/icons'
import { usePolling } from '../hooks/usePolling'
import { useSSE } from '../hooks/useSSE'
import client from '../api/client'

const { Title, Text } = Typography

/**
 * One unit for one list (G28). The -api stores epoch SECONDS; the gateway stores
 * UTC datetimes, but SQLite drops the offset, so the API returns them NAIVE
 * ("2026-09-25 07:28:08.866107") and `Date.parse` would read them as LOCAL time.
 * In UTC+8 that is an 8-hour error — invisible in the `Updated` column, which
 * shifts both ways and cancels out, but plain in `Elapsed`, which measures
 * against `Date.now()`: a job one minute old rendered as "8h 0m".
 */
const epochSeconds = (v: any): number | undefined => {
  if (v == null) return undefined
  if (typeof v === 'number') return v
  const s = String(v).trim()
  if (!s) return undefined
  const hasZone = /[Zz]$|[+-]\d\d:?\d\d$/.test(s)
  const ms = Date.parse(hasZone ? s : `${s.replace(' ', 'T')}Z`)
  return Number.isNaN(ms) ? undefined : ms / 1000
}

const STATUS_COLORS: Record<string,string> = {
  pending:'default', running:'processing', completed:'success', succeeded:'success',
  failed:'error', cancelled:'warning',
  // Gateway generation-job statuses. `paused` is TERMINAL on its leg (G27) and
  // `unknown` is the -api's own (G28) — one list, so both appear here.
  queued:'default', awaiting_input:'warning', paused:'warning', unknown:'default',
}

export default function TasksPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [tasks, setTasks] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [logTaskId, setLogTaskId] = useState<string | null>(null)
  const [logDrawerOpen, setLogDrawerOpen] = useState(false)

  // ---- Gateway generation jobs (a SECOND, distinct task family) -----------
  // These live in the gateway's own store, not the -api's task table, so they
  // never appeared here — which is why a pre-deploy card's `Job: #N` link could
  // not reach anything: the id is a generation-job id and this page listed only
  // -api tasks, and ignored the `?job=` parameter outright.
  const [genJobs, setGenJobs] = useState<any[]>([])
  const [genJob, setGenJob] = useState<any>(null)
  const genPollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Only the FIRST load may raise the table's spinner. This page re-polls every
  // 5 s (`usePolling` below), and re-raising `loading` on each poll made the
  // whole table blank-and-refill on a timer — the list is already on screen; a
  // refresh is not a navigation. The ref is what makes "first" mean first.
  const firstLoadRef = useRef(true)
  const fetchTasks = useCallback(async () => {
    if (firstLoadRef.current) setLoading(true)
    try {
      const { data } = await client.get('/tasks')
      const list = data.tasks || data || []
      setTasks(Array.isArray(list) ? list : [])
    } catch { /* silent */ }
    finally {
      if (firstLoadRef.current) { firstLoadRef.current = false; setLoading(false) }
    }
  }, [])

  const fetchGenJobs = useCallback(async () => {
    try {
      const { data } = await client.get('/llm/jobs')
      setGenJobs(data.jobs || [])
    } catch { /* silent */ }
  }, [])

  useEffect(() => { fetchTasks(); fetchGenJobs() }, [fetchTasks, fetchGenJobs])
  usePolling(() => { fetchTasks(); fetchGenJobs() }, 5000)

  // There is deliberately NO `Answer` action on this page (user ruling
  // 2026-09-26). A pause is answered on the surface that OWNS the work: a
  // deploy-triggered pause has its service card, and the standalone Generate
  // path — which creates no session and so no card — is answered from its own
  // row on the Source Projects page, whose button slot turns into `Check`. Two
  // answering surfaces for one pause is how an operator ends up answering the
  // wrong one of two views of the same job.

  const cancelTask = async (id: string) => {
    try { await client.delete(`/tasks/${id}`); message.success('Cancelled'); fetchTasks() }
    catch { message.error('Failed to cancel') }
  }

  /** Cancel a GATEWAY generation job (G27/G31: cancellation lands on the row, so
   * it stops the running leg wherever it is being executed). */
  const cancelGenJob = async (id: number) => {
    try { await client.delete(`/llm/jobs/${id}`); message.success('Cancelled'); fetchGenJobs() }
    catch { message.error('Failed to cancel') }
  }

  // --- -api task log (SSE) ---
  const sseUrl = logTaskId ? `/api/tasks/${logTaskId}/log` : null
  const { lines: logLines, isConnected: logConnected, clearLines: clearLog } = useSSE(sseUrl)

  const openLogs = useCallback((taskId: string) => {
    setGenJob(null)
    setLogTaskId(taskId)
    setLogDrawerOpen(true)
    clearLog()
  }, [clearLog])

  /** Open a GATEWAY generation job's own step log (events), polling while live. */
  const openGenJob = useCallback((id: number) => {
    setLogTaskId(null)
    setGenJob({ id, status: 'loading', events: [] })
    setLogDrawerOpen(true)
    const tick = async () => {
      try {
        const { data } = await client.get(`/llm/jobs/${id}`)
        setGenJob(data)
        // A job that has stopped is not going to produce more events — but the
        // poll must not stop on `awaiting_input`, which is NOT terminal.
        if (['completed','failed','cancelled'].includes(data.status) && genPollRef.current) {
          clearInterval(genPollRef.current); genPollRef.current = null
        }
      } catch { /* keep the last view */ }
    }
    tick()
    if (genPollRef.current) clearInterval(genPollRef.current)
    genPollRef.current = setInterval(tick, 2000)
  }, [])

  // Deep links: `?job=` (gateway generation job) and `?task=` (-api task log).
  // The pre-deploy card links to both.
  const jobParam = searchParams.get('job') || ''
  const taskParam = searchParams.get('task') || ''
  useEffect(() => {
    if (jobParam) openGenJob(Number(jobParam))
    else if (taskParam) openLogs(taskParam)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobParam, taskParam])

  useEffect(() => () => { if (genPollRef.current) clearInterval(genPollRef.current) }, [])

  const closeLogs = () => {
    setLogDrawerOpen(false)
    setLogTaskId(null)
    setGenJob(null)
    if (genPollRef.current) { clearInterval(genPollRef.current); genPollRef.current = null }
    clearLog()
    if (jobParam || taskParam) setSearchParams({})
  }

  const formatElapsed = (createdAt: number) => {
    if (!createdAt) return '-'
    const elapsed = Math.floor((Date.now() / 1000) - createdAt)
    if (elapsed < 60) return `${elapsed}s`
    if (elapsed < 3600) return `${Math.floor(elapsed/60)}m ${elapsed%60}s`
    const h = Math.floor(elapsed/3600)
    const m = Math.floor((elapsed%3600)/60)
    return `${h}h ${m}m`
  }

  // --- ONE list (G28) -------------------------------------------------------
  // Both families are the same SHAPE (an id, a type, a status, a target, two
  // timestamps) and each owner keeps its own store — the gateway SQLite, the
  // -api its JSON — so this page merges them for DISPLAY only. The two-table
  // split this replaces made a `gt-` job visible on one table and an `api-` task
  // on the other, which is how a card's `Job:` link ended up pointing at a page
  // that could not show it. The ids are self-describing by construction
  // (`gt-` / `api-`), and each row carries its source tag.
  type Row = {
    // `core` is the DISPLAY name of the -api family (user ruling 2026-09-26).
    // DISPLAY ONLY: its records keep their `api-…` ids — the id is the record's
    // name, and existing rows keep theirs (GRILLING G28's `gt-`/`api-` note
    // stands untouched).
    key: string; source: 'gateway' | 'core'; id: string; type: string
    target: string; status: string; progress?: string
    created_at?: number; updated_at?: number; raw: any
  }

  const rows: Row[] = [
    ...genJobs.map((j: any): Row => ({
      key: `gt-${j.id}`,
      source: 'gateway' as const,
      id: `gt-${j.id}`,                       // what the card's `Job:` link names
      type: j.job_type === 'fix' ? 'LLM analysis' : 'generate',
      target: `${j.service_name || ''}${j.recipe_path ? `@@${j.recipe_path}` : ''}`,
      status: j.status,
      progress: j.progress,
      created_at: epochSeconds(j.created_at),
      updated_at: epochSeconds(j.updated_at),
      raw: j,
    })),
    ...tasks.map((t: any): Row => ({
      key: t.task_id,
      source: 'core' as const,
      id: t.task_id,
      type: t.type || 'task',
      target: (t.result && typeof t.result === 'object')
        ? `${t.result.user_name || ''}${t.result.service_name ? `/${t.result.service_name}` : ''}${t.result.label ? `/${t.result.label}` : ''}`
        : (t.type || 'task'),
      status: t.status,
      progress: undefined,
      created_at: epochSeconds(t.created_at),
      updated_at: epochSeconds(t.updated_at),
      raw: t,
    })),
  ].sort((a, b) => (b.created_at || 0) - (a.created_at || 0))

  const columns = [
    { title:'Source', dataIndex:'source', key:'source', width:90,
      render:(s:string)=><Tag color={s === 'gateway' ? 'geekblue' : 'default'}>{s}</Tag> },
    { title:'ID', dataIndex:'id', key:'id', width:130, ellipsis:true,
      render:(v:string)=>v||'-' },
    { title:'Type', dataIndex:'type', key:'type', width:130,
      render:(t:string)=><Tag color={t === 'LLM analysis' ? 'purple' : undefined}>{t||'task'}</Tag> },
    { title:'Target', dataIndex:'target', key:'target', width:160, ellipsis:true,
      render:(v:string)=><span>{v||'-'}</span> },
    { title:'Status', dataIndex:'status', key:'status', width:110,
      render:(s:string)=><Tag color={STATUS_COLORS[s]||'default'}>{s||'unknown'}</Tag> },
    { title:'Progress', dataIndex:'progress', key:'progress', ellipsis:true,
      render:(p:string)=><Text type="secondary" style={{fontSize:12}}>{p||'-'}</Text> },
    { title:'Updated', dataIndex:'updated_at', key:'updated_at', width:160,
      render:(v:number)=>v?new Date(v*1000).toLocaleString():'-' },
    { title:'Elapsed', key:'elapsed', width:100,
      render:(_:any,r:Row)=>formatElapsed(r.created_at || 0) },
    { title:'Actions', key:'actions', width:170,
      render:(_:any,r:Row)=> r.source === 'gateway' ? <Space>
        <Button size="small" icon={<EyeOutlined/>} onClick={()=>openGenJob(r.raw.id)}>Logs</Button>
        {/* A generation can be cancelled from here (T20): the operator's only
            route to free the gateway's own pool. No Delete — the gateway has no
            such endpoint; records leave by cancellation or the TTL.
            The status set is the SERVER's `job_live` (`pre_deploy.py`) and not a
            second opinion: T14 requires cancel to appear only while a job is
            genuinely live and to DISAPPEAR when it stops. `paused` is terminal
            under G27, so a paused leg has stopped — offering cancel there would
            contradict the card, which already hides it. */}
        {['queued','running','awaiting_input'].includes(r.status) &&
          <Popconfirm title="Cancel?" onConfirm={()=>cancelGenJob(r.raw.id)}>
            <Button size="small" danger icon={<PauseCircleOutlined/>}/>
          </Popconfirm>}
        {/* A paused leg has STOPPED, so no action is offered here at all. It is
            answered where it was created — the service card, or the Source
            Projects row for the session-less Generate path. */}
      </Space> : <Space>
        <Button size="small" icon={<EyeOutlined/>} onClick={()=>openLogs(r.raw.task_id)}>Logs</Button>
        {/* ONE rule for both families (G28's "one list"): the cancel-ish action
            appears only while the work is genuinely live. The old button said
            "Delete" and called `DELETE /api/tasks/{id}` — which is the -api's
            CANCEL, so for any terminal row it answered `409 Task already in
            terminal state`, the client swallowed the rejection, and the list
            refreshed as if it had worked. A record leaves by the cleanup
            contract (G30), not by a click. */}
        {(r.status==='pending'||r.status==='running')&&<Popconfirm title="Cancel?" onConfirm={()=>cancelTask(r.raw.task_id)}><Button size="small" danger icon={<PauseCircleOutlined/>}/></Popconfirm>}
      </Space> },
  ]

  // The generation job's step log: its `events` are the popup's append-only step
  // rows, rendered here as one line each.
  const genLines = (): string[] => {
    if (!genJob) return []
    const out: string[] = []
    out.push(`job #${genJob.id}  status=${genJob.status}  phase=${genJob.phase ?? '-'}  ${genJob.progress || ''}`)
    if (genJob.error) out.push(`ERROR: ${genJob.error}`)
    for (const e of (genJob.events || [])) {
      const when = e.ts ? new Date(e.ts).toLocaleTimeString() : ''
      const bits = [when, e.file, e.status, e.purpose].filter(Boolean).join('  ')
      out.push(bits)
      for (const err of (e.errors || [])) out.push(`    ✗ ${err}`)
      if (e.guide) out.push(`    guide: ${String(e.guide).slice(0, 400)}`)
      if (e.conflicts?.length) out.push(`    conflicts: ${e.conflicts.map((c:any)=>c.name).join(', ')}`)
    }
    return out
  }

  const drawerLines = genJob ? genLines() : logLines
  const drawerTitle = genJob
    ? <Space><Tag color="blue">Generation job</Tag>
        <Tag color={STATUS_COLORS[genJob.status]||'default'}>{genJob.status}</Tag>
        <span>#{genJob.id}</span></Space>
    : (logConnected ? <Tag color="green">Live</Tag> : <Tag color="default">Disconnected</Tag>)

  return (
    <div>
      <div style={{display:'flex',justifyContent:'space-between',marginBottom:16,flexWrap:'wrap',gap:8}}>
        <Title level={3} style={{margin:0}}>Tasks</Title>
        <Button icon={<ReloadOutlined/>} onClick={()=>{fetchTasks(); fetchGenJobs()}}>Refresh</Button>
      </div>

      <Title level={5} style={{marginTop:0}}>All tasks <Text type="secondary" style={{fontSize:12,fontWeight:'normal'}}>— ONE list holding both families: the gateway's generation jobs (<Text code>gt-</Text>) and the core task queue (<Text code>api-</Text> ids), newest first</Text></Title>
      <Table
        dataSource={rows}
        columns={columns}
        rowKey={(r:Row)=>r.key}
        loading={loading}
        pagination={{pageSize:20,showSizeChanger:true,showTotal:(t)=>`${t} records`}}
        size="small"
        scroll={{x:1100}}
        locale={{emptyText:'Nothing running.'}}
      />

      {/* Log drawer — a generation job's step log, or a -api task's live log */}
      <Drawer
        title={drawerTitle}
        placement="right"
        width={700}
        open={logDrawerOpen}
        onClose={closeLogs}
      >
        {genJob && (
          <div style={{marginBottom:8}}>
            <Text type="secondary" style={{fontSize:12}}>
              Service <Text code>{genJob.service_name}</Text> · recipe <Text code>{genJob.recipe_path || '(root)'}</Text>
              {genJob.updated_at ? ` · updated ${new Date(genJob.updated_at).toLocaleString()}` : ''}
            </Text>
          </div>
        )}
        <div style={{
          background:'#1e1e1e', color:'#d4d4d4', fontFamily:'monospace', fontSize:12,
          padding:12, borderRadius:4, maxHeight:'calc(100vh - 200px)', overflow:'auto',
          whiteSpace:'pre-wrap', wordBreak:'break-all'
        }}>
          {drawerLines.length===0 ? <span style={{color:'#888'}}>Waiting for logs...</span> :
            drawerLines.map((line,i)=><div key={i}>{line}</div>)}
        </div>
      </Drawer>
    </div>
  )
}
