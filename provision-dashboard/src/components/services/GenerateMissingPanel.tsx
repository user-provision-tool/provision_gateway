import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Divider, Input, Modal, Progress, Select, Space, Spin, Tag, Typography, message } from 'antd'
import { CheckCircleTwoTone, WarningOutlined } from '@ant-design/icons'
import Editor from '@monaco-editor/react'
import client from '../../api/client'
import { deriveProfiles } from '../../api/services'
import { useGenerationJob, isPaused } from '../../hooks/useGenerationJob'
import PausePrompt from './PausePrompt'
import BaseFilesPopup, {
  BaseRefs,
  EMPTY_BASE_REFS,
  SOURCE_PROJECT_REF_SLOTS,
  activeBaseRefs,
} from './BaseFilesPopup'
import AdditionalContextPopup, {
  AdditionalContext,
} from './AdditionalContextPopup'
import { setServiceContext, useServiceContext } from '../../stores/additionalContext'
import FileSetSelection, {
  phaseOfFile,
  SelectedFileRow,
  EMPTY_CANDIDATES,
  EMPTY_SELECTION,
  FileSetCandidates,
  FileSetSelectionData,
  FileSetStale,
  userVariantFor,
} from './FileSetSelection'

const { Text } = Typography

interface Props {
  open: boolean
  serviceName: string
  recipePath: string
  /**
   * A paused job to re-attach to on open — the Source Projects row's `Check`
   * sends this so the pause popup comes back and the leg can be answered and
   * resumed (GRILLING G27: a pause is a terminal leg, and answering it starts
   * the next one).
   */
  resumeJobId?: string | null
  onClose: () => void
  onChanged: () => void
}

/**
 * LLM generate-missing panel (design §Selection & UI): opened by the
 * "Missing files" action on the source-projects page. Full selection +
 * prompt UI, async generation job with progress polling, review/edit gate
 * with per-phase validation display, and save (overwrite matrix).
 * No-LLM mode: selection stays usable, prompt + generate greyed with the
 * fallback-strategy alert.
 */
export default function GenerateMissingPanel({ open, serviceName, recipePath, resumeJobId, onClose, onChanged }: Props) {
  const [loading, setLoading] = useState(true)
  const [llmActive, setLlmActive] = useState(false)
  const [candidates, setCandidates] = useState<FileSetCandidates>(EMPTY_CANDIDATES)
  const [stale, setStale] = useState<FileSetStale>({})
  const [selection, setSelection] = useState<FileSetSelectionData>(EMPTY_SELECTION)
  const [needsEnv, setNeedsEnv] = useState(true)
  const [prompt, setPrompt] = useState('')
  // R26/R29 — this panel's classification picker is CLASSIFY-ONLY: `generate
  // new` runs the recipe classification (no `.env.raw`, no deploy identity);
  // an existing variant is selectable/editable for inspection and is used by a
  // later deploy.
  const [classificationChoice, setClassificationChoice] = useState('')
  const [classificationState, setClassificationState] = useState<any>(null)
  // Bumped after a variant edit/save so the schema/coverage checks re-run.
  const [classificationRefresh, setClassificationRefresh] = useState(0)
  const clsVariants: any[] = classificationState?.classification?.variants || []
  const clsVariantKeys = clsVariants.map((v: any) => v.value).join('|')
  const clsDefault: string = classificationState?.classification?.default || 'generate_new'
  const clsDefaultSafe = clsVariants.find((v: any) => v.value === clsDefault)?.red
    ? 'generate_new'
    : clsDefault
  // C20 — a selection from another fingerprint is not a valid state.
  useEffect(() => {
    if (!classificationChoice || classificationChoice === 'none'
        || classificationChoice === 'generate_new') return
    if (classificationState && !clsVariants.some((v: any) => v.value === classificationChoice)) {
      setClassificationChoice('')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [classificationChoice, clsVariantKeys, classificationState])
  const effectiveClassification = classificationChoice || clsDefaultSafe
  const clsSelected = clsVariants.find((v: any) => v.value === effectiveClassification)
  const clsIsExisting = effectiveClassification.startsWith('env.classification.')
  // R1/R3/C5 — the Source Projects modal mirrors its THREE sent-file pickers
  // and has no Env slot; the arbitrary context is shared with the deploy panel.
  const [baseRefs, setBaseRefs] = useState<BaseRefs>(EMPTY_BASE_REFS)
  // C16 — shared per-service context store (the project page writes it too).
  const additionalContext = useServiceContext(serviceName)
  const setAdditionalContext = (ctx: AdditionalContext) => setServiceContext(serviceName, ctx)
  const [basePopupOpen, setBasePopupOpen] = useState(false)
  const [contextPopupOpen, setContextPopupOpen] = useState(false)
  const { jobId, jobState, polling, startJob, cancelJob, submitInput, resumeJob, reset } = useGenerationJob()

  // Review/edit gate (design §Generation rules — human gate is final).
  const [reviewFiles, setReviewFiles] = useState<Record<string, string> | null>(null)
  const [validations, setValidations] = useState<Record<string, any>>({})
  const [editorFile, setEditorFile] = useState<string | null>(null)
  const [editorContent, setEditorContent] = useState('')
  const [editorFromPicker, setEditorFromPicker] = useState(false)
  const [saving, setSaving] = useState(false)
  // The resume target already attached in THIS open (see the init effect).
  const resumedRef = useRef<string | null>(null)

  useEffect(() => {
    if (!open) { resumedRef.current = null; return }
    setLoading(true)
    setReviewFiles(null)
    setValidations({})
    setPrompt('')
    setBaseRefs(EMPTY_BASE_REFS)
    setBasePopupOpen(false)
    setContextPopupOpen(false)
    reset()
    // Re-attach to a paused leg (the row's `Check`). The job runs in the GATEWAY,
    // not the browser, so it outlives the panel that started it — without this
    // the pause would have no surface here and nothing could answer it.
    //
    // ONCE per open, not on every run of this effect: it also fires when the
    // operator changes the SERVICE inside the panel, and re-attaching then
    // would show the previous recipe's pause popup over the new recipe's
    // pickers. The ref is cleared on close, so reopening the row's `Check`
    // attaches again.
    if (resumeJobId != null && resumedRef.current !== resumeJobId) {
      resumedRef.current = resumeJobId
      resumeJob(resumeJobId)
    }
    const params: Record<string, string> = {}
    if (recipePath) params.recipe_path = recipePath
    Promise.all([
      client.get(`/projects/${serviceName}/file-sets`).then(r => r.data),
      client.get('/llm/config').then(r => r.data).catch(() => ({ is_active: false })),
      client.get(`/projects/${serviceName}/check-missing-files`, { params })
        .then(r => r.data).catch(() => ({ needs_env: true })),
    ]).then(([fs, cfg, check]: any[]) => {
      const rp = recipePath || '.'
      const entry = (fs.file_sets || {})[rp] || {}
      const cand = {
        ...((fs.candidates || {})[rp] || EMPTY_CANDIDATES),
        provenance: (fs.provenance || {})[rp] || {},
      }
      setCandidates(cand)
      setStale((fs.stale || {})[rp] || {})
      setSelection({
        compose: entry.compose || [],
        nginx: entry.nginx || null,
        stream_conf: entry.stream_conf || null,
        mail_conf: entry.mail_conf || null,
        env: entry.env || [],
        profiles: entry.profiles || [],
      })
      setNeedsEnv(check.needs_env !== false)
      setLlmActive(!!(cfg && (cfg.is_active || cfg.byok_configured)))
    }).catch(() => message.error('Failed to load file selection'))
      .finally(() => setLoading(false))
  }, [open, serviceName, recipePath, resumeJobId])

  // Profiles recompute (GAP-2): profile candidates derive from the merged
  // compose — when the in-panel compose selection changes, refetch candidates
  // so the profiles section follows the selection (design §Selection & UI
  // L59-62) instead of staying stale from the stored file set.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    if (selection.compose.length === 0) {
      setCandidates(prev => ({ ...prev, profiles: [] }))
      return
    }
    deriveProfiles(serviceName, selection.compose, recipePath)
      .then(({ data }) => {
        if (cancelled) return
        const profiles: string[] = (data.candidates && data.candidates.profiles) || []
        setCandidates(prev => ({ ...prev, profiles }))
        // A profile absent from the currently selected compose cannot be
        // activated — drop it from the selection (in-panel stale semantics).
        setSelection(prev => ({
          ...prev,
          profiles: prev.profiles.filter(p => profiles.includes(p)),
        }))
      })
      .catch(() => {
        if (!cancelled) setCandidates(prev => ({ ...prev, profiles: [] }))
      })
    return () => { cancelled = true }
  }, [open, serviceName, recipePath, selection.compose])

  // R26/R29 — the classify-only picker state, recomputed with the compose
  // selection (the structural fingerprint's compose input).
  useEffect(() => {
    if (!open) return
    let cancelled = false
    client.get(`/projects/${serviceName}/classification-state`, {
      params: {
        recipe_path: recipePath,
        compose: selection.compose.join(','),
        env: '',
        env_none: false,
        // R26/R29 — the target allowlist is a deploy-time constraint; this
        // panel has no deploy identity, so only schema/coverage red applies.
        classify_only: true,
        current: classificationChoice,
      },
    }).then(({ data }) => {
      if (!cancelled) setClassificationState(data)
    }).catch(() => { if (!cancelled) setClassificationState(null) })
    return () => { cancelled = true }
  }, [open, serviceName, recipePath, selection.compose.join(','), classificationChoice,
      classificationRefresh])

  // Job completed → open the review gate with the result + per-phase validation.
  useEffect(() => {
    if (polling || !jobState || jobState.status !== 'completed') return
    const files = (jobState.result && jobState.result.files) || {}
    setValidations((jobState.result && jobState.result.validations) || {})
    setReviewFiles(files)
  }, [jobState, polling])

  const effectiveSelection = (): FileSetSelectionData => ({
    compose: selection.compose.filter(p => !(stale.compose || []).includes(p)),
    nginx: selection.nginx && !(stale.nginx || []).includes(selection.nginx) ? selection.nginx : null,
    // G55 — the stream/mail categories travel with the selection, same rule.
    stream_conf: selection.stream_conf
      && !(stale.stream_conf || []).includes(selection.stream_conf)
      ? selection.stream_conf : null,
    mail_conf: selection.mail_conf
      && !(stale.mail_conf || []).includes(selection.mail_conf)
      ? selection.mail_conf : null,
    // This panel has no interpolation env slot; env is deploy-time. The
    // reference files travel as `env_reference`, never as selection.env.
    env: [],
    // Recipe-level generation is profile-blind; the -api filters conf blocks
    // at render. The standalone panel has no profile picker.
    profiles: [],
    // R4-F62 — the serving-ports slot must travel here too, and it did NOT.
    //
    // This panel renders the serving-ports picker (it uses the same
    // `FileSetSelection` as the deploy panel), so the operator could select an
    // artifact, watch it appear in the panel, and have it silently dropped from
    // the request: the job's `selection` arrived without a `serving_ports` key,
    // so `validate_ports` was never in its phase plan (its "in play" test reads
    // that key) and the artifact was neither checked nor used. Measured
    // 2026-10-11 on `probe-validate-standalone`: the job completed WITHOUT the
    // check ever running.
    //
    // The same hand-rebuilt key list as the deploy panel's R2-F39 and the save
    // path's R2-F33 — this is the third instance of the shape, which is why the
    // comment is repeated rather than referenced.
    serving_ports: selection.serving_ports
      && !(stale.serving_ports || []).includes(selection.serving_ports)
      ? selection.serving_ports
      : null,
    // R1.3 — and the deliberate-None state goes with it, never both.
    serving_ports_none: !selection.serving_ports && !!selection.serving_ports_none,
  })

  const openSelectedFile = async (name: string) => {
    const path = recipePath ? `${recipePath}/${name}` : name
    try {
      const { data } = await client.get(`/projects/${serviceName}/files/${path}`)
      setEditorFile(name)
      setEditorContent(typeof data?.content === 'string' ? data.content : '')
      setEditorFromPicker(true)
    } catch (err: any) {
      message.error(err.response?.data?.detail || `Could not open ${name}`)
    }
  }

  // T6 — the job result carries `{name, sha256, size}` METADATA, never
  // content. Review/edit fetches the file by name (the metadata object must
  // never reach the editor, and must never be written back).
  const openReviewFile = async (name: string) => {
    const path = recipePath ? `${recipePath}/${name}` : name
    try {
      const { data } = await client.get(`/projects/${serviceName}/files/${path}`)
      setEditorFile(name)
      setEditorContent(typeof data?.content === 'string' ? data.content : '')
      setEditorFromPicker(false)
    } catch (err: any) {
      message.error(err.response?.data?.detail || `Could not open ${name}`)
    }
  }

  const saveEditor = async () => {
    if (!editorFile) return
    if (editorFromPicker) {
      const targetName = userVariantFor(editorFile)
      try {
        await client.post('/projects/save-generated', {
          service_name: serviceName,
          recipe_path: recipePath,
          files: { [targetName]: editorContent },
        })
        setSelection(prev => ({
          ...prev,
          compose: prev.compose.map(p => (p === editorFile ? targetName : p)),
          nginx: prev.nginx === editorFile ? targetName : prev.nginx,
          stream_conf: prev.stream_conf === editorFile ? targetName : prev.stream_conf,
          mail_conf: prev.mail_conf === editorFile ? targetName : prev.mail_conf,
          serving_ports: prev.serving_ports === editorFile ? targetName : prev.serving_ports,
          env: prev.env.map(p => (p === editorFile ? targetName : p)),
        }))
        message.success(`Saved as ${targetName} — the selection now uses it`)
        setEditorFile(null)
        setEditorContent('')
        setEditorFromPicker(false)
      } catch (err: any) {
        message.error(err.response?.data?.detail || 'Failed to save file')
      }
      return
    }
    if (reviewFiles) {
      setReviewFiles({ ...reviewFiles, [editorFile]: editorContent })
      setEditorFile(null)
      setEditorContent('')
      return
    }
    // A generated classification/relation variant opened from the picker:
    // it carries its `.generated` marker, so save in place (never fork).
    try {
      await client.post('/projects/save-generated', {
        service_name: serviceName,
        recipe_path: recipePath,
        files: { [editorFile]: editorContent },
      })
      message.success(`${editorFile} saved`)
      setEditorFile(null)
      setEditorContent('')
      setClassificationRefresh(n => n + 1)
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to save file')
    }
  }

  const startGeneration = async () => {
    if (!llmActive) return
    try {
      await startJob({
        service_name: serviceName,
        recipe_path: recipePath,
        job_type: 'generate_missing',
        skip_env: true,  // env is generated per-user at deploy time (§8.7)
        // R26/R29 — classify-only: the env phase runs the classification but
        // writes no `.env.raw` and no deploy identity. Under `None` it does
        // nothing at all.
        classification: effectiveClassification,
        classify_only: true,
        prompt,
        // C5 — three slots, no Env slot on this surface.
        base_refs: { ...activeBaseRefs(baseRefs, effectiveSelection()), env: [] },
        context: additionalContext,
        selection: effectiveSelection(),
        deploy_metadata: { service_name: serviceName, user_name: '', label: '0', domain: 'localhost' },
      })
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to start generation job')
    }
  }

  const saveReview = async () => {
    if (!reviewFiles) return
    setSaving(true)
    try {
      const { data } = await client.post('/projects/save-generated', {
        service_name: serviceName,
        recipe_path: recipePath,
        files: reviewFiles,
        selection: effectiveSelection(),
      })
      message.success(`Saved ${(data.saved || []).length} generated file(s) to ${serviceName}`)
      onChanged()
      setReviewFiles(null)
      reset()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to save generated files')
    } finally {
      setSaving(false)
    }
  }

  // Which phase produced this file — read off the NAME's SHAPE (`phaseOfFile`),
  // never off an exact-name table. A generated compose/nginx name carries the
  // job's collision token (`docker-compose.base.337.yml`, GRILLING G33), and the
  // review gate reads a MISSING validation as "valid" (`ok = !v || v.valid`), so
  // a table of exact names would fail SILENTLY: every file green.
  const validationFor = (filename: string) => (validations || {})[phaseOfFile(filename)]

  const jobProgress = (jobState && jobState.phase_total && jobState.phase_total > 1)
    ? Math.round(((jobState.phase_index || 0) + (jobState.status === 'running' ? 0.5 : 0)) / jobState.phase_total * 100)
    : jobState && (jobState.status === 'running' || jobState.status === 'queued') ? 10 : 0

  const eff = effectiveSelection()
  const activeRefs = activeBaseRefs(baseRefs, eff)
  const refRows: { name: string; slot: 'compose' | 'serving_ports' | 'nginx'; cat?: 'http' | 'stream' | 'mail' }[] = []
  activeRefs.compose.forEach(name => refRows.push({ name, slot: 'compose' }))
  if (activeRefs.serving_ports) refRows.push({ name: activeRefs.serving_ports, slot: 'serving_ports' })
  ;(['http', 'stream', 'mail'] as const).forEach(cat => {
    const n = activeRefs.nginx[cat]
    if (n) refRows.push({ name: n, slot: 'nginx', cat })
  })
  const removeRef = (name: string, slot: 'compose' | 'serving_ports' | 'nginx',
                     cat?: 'http' | 'stream' | 'mail') => {
    if (slot === 'compose') setBaseRefs({ ...baseRefs, compose: baseRefs.compose.filter(x => x !== name) })
    else if (slot === 'nginx') setBaseRefs({ ...baseRefs, nginx: { ...baseRefs.nginx, [cat || 'http']: null } })
    else setBaseRefs({ ...baseRefs, serving_ports: null })
  }

  return (
    <Modal
      title={`Generate Base Files${serviceName ? `: ${serviceName}${recipePath ? ` @ ${recipePath}` : ''}` : ''}`}
      open={open}
      onCancel={onClose}
      footer={null}
      width={760}
      destroyOnClose
    >
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        {loading ? <div style={{ textAlign: 'center', padding: 24 }}><Spin /></div> : (<>
          <Alert
            type="info"
            showIcon
            message="Env files are NOT generated here"
            description="Per-user env files are generated automatically at deploy time (last step of the deploy pipeline). This panel generates the recipe-level artifacts — compose, serving_ports and nginx — and can run a classify-only env classification (no .env.raw, no deploy identity)."
          />
          {!llmActive && (
            <Alert
              type="warning"
              showIcon
              message="No LLM configured — no-LLM fallback strategy in effect"
              description="The selection/skeleton/manual flows remain usable; the prompt and generate actions are greyed out. Generate is disabled: use the file editor to add files manually, or deploy with a warning."
            />
          )}

          <FileSetSelection
            selection={selection}
            candidates={candidates}
            stale={stale}
            needsEnv={needsEnv}
            showEnv={false}
            showProfiles={false}
            onChange={setSelection}
            onOpenFile={openSelectedFile}
          />

          {/* R26/R29 — the classify-only classification picker. It runs the
              recipe classification with no `.env.raw`; a later deploy uses the
              selected variant. */}
          <div>
            <div style={{ fontSize: 13, marginBottom: 4 }}>Env classification (classify only)</div>
            {clsIsExisting && (
              <SelectedFileRow
                name={effectiveClassification}
                onOpen={(n) => openReviewFile(n)}
                onRemove={() => setClassificationChoice('')}
                stale={!!clsSelected?.red}
              />
            )}
            <Select
              style={{ width: '100%' }}
              value={effectiveClassification}
              onChange={(v) => setClassificationChoice(String(v))}
              options={[
                { value: 'none', label: 'None — no classification' },
                { value: 'generate_new', label: 'generate new' },
                ...clsVariants.map((v) => ({
                  value: v.value,
                  title: v.value,
                  label: (
                    <span>
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
              `generate new` classifies the recipe over its own env files; no
              per-user `.env.raw` is written here. An existing variant is
              selectable and editable; Generate regenerates only red ones.
            </div>
            {(clsSelected?.errors?.length > 0 || clsSelected?.missing_env_keys?.length > 0) && (
              <Alert type="error" showIcon style={{ marginTop: 8 }}
                message="This classification cannot be used"
                description={`According to the classification, ${[
                  ...(clsSelected?.errors || []),
                  ...((clsSelected?.missing_env_keys || []).length
                    ? [`${(clsSelected?.missing_env_keys || []).join(', ')} ${(clsSelected?.missing_env_keys || []).length === 1 ? 'is' : 'are'} missing`]
                    : []),
                ].join('; ')}. The env classification will be regenerated because the currently selected one is invalid.`} />
            )}
          </div>

          <Divider plain style={{ margin: '8px 0' }}>Generation prompt</Divider>
          <Input.TextArea
            rows={3}
            disabled={!llmActive}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Describe the deployment — REQUIRED when the recipe has no base files at all (empty recipe). You may include doc URLs; the agent fetches them."
          />

          <Space size="small" style={{ marginTop: 8 }} wrap>
            <Button size="small" onClick={() => setBasePopupOpen(true)}>⚙ base files</Button>
            <Button size="small" onClick={() => setContextPopupOpen(true)}>+ context</Button>
          </Space>
          <Alert
            type="info"
            showIcon
            message="select a base to put hard constraint on how a file should be like"
          />
          {(refRows.length > 0 || additionalContext.files.length > 0
            || additionalContext.links.length > 0) && (
            <div>
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

          {jobState && (
            <div style={{ border: '1px solid #f0f0f0', borderRadius: 8, padding: 12 }}>
              {jobState.status === 'completed' ? (
                reviewFiles && (
                  <Space direction="vertical" style={{ width: '100%' }} size="small">
                    <Text strong>Generated files — review &amp; edit, then save</Text>
                    {Object.keys(reviewFiles).map(fn => {
                      const v = validationFor(fn)
                      const ok = !v || v.valid === true
                      return (
                        <div key={fn} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <Tag color={ok ? 'green' : 'red'} style={{ marginRight: 0 }}>
                            {ok ? '✓ valid' : '✗ invalid'}
                          </Tag>
                          <span style={{ fontFamily: 'monospace', fontSize: 13 }}>{fn}</span>
                          <Button size="small" onClick={() => { void openReviewFile(fn) }}>Edit</Button>
                          {!ok && (
                            <Text type="danger" style={{ fontSize: 12, flex: 1 }}>
                              {((v && v.errors) || []).join('; ').slice(0, 200)}
                            </Text>
                          )}
                        </div>
                      )
                    })}
                    <Space style={{ marginTop: 8 }}>
                      <Button type="primary" loading={saving} onClick={saveReview}>Save to project</Button>
                      <Button onClick={onClose}>Close</Button>
                    </Space>
                  </Space>
                )
              ) : isPaused(jobState) ? (
                // A paused job started HERE must be answerable here — the prompt is the shared
                // one, so the pause is not a dead end on this surface (task doc §5).
                // `isPaused` (either shape) and not `status === 'awaiting_input'`:
                // G27 made a pause TERMINAL, so it arrives as `paused` and the
                // legacy status check matched nothing — the panel then rendered
                // the progress branch for a stopped leg, with no way to answer it.
                <PausePrompt
                  jobState={jobState}
                  onSubmit={submitInput}
                  onCancel={cancelJob}
                />
              ) : (
                <Space direction="vertical" style={{ width: '100%' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <Text strong>
                      {jobState.status === 'cancelled' ? 'Job cancelled' :
                       jobState.status === 'failed' ? `Job failed${jobState.error ? `: ${jobState.error}` : ''}` :
                       `Generating… ${jobState.phase ? `phase ${jobState.phase_index + 1}/${jobState.phase_total} (${jobState.phase})` : ''}`}
                    </Text>
                    {(jobState.status === 'queued' || jobState.status === 'running') && (
                      <Button size="small" danger onClick={cancelJob}>Cancel</Button>
                    )}
                  </div>
                  <Progress percent={jobProgress} size="small" status={jobState.status === 'failed' ? 'exception' : 'active'} />
                  <Text type="secondary" style={{ fontSize: 12 }}>{jobState.progress || ''}</Text>
                </Space>
              )}
            </div>
          )}


          <Space style={{ justifyContent: 'flex-end', width: '100%' }}>
            <Button onClick={onClose}>Close</Button>
            <Button
              type="primary"
              icon={<span>✨</span>}
              disabled={!llmActive || polling || jobId !== null}
              loading={polling}
              onClick={startGeneration}
            >
              Generate Base Files
            </Button>
          </Space>
        </>)}
      </Space>

      {/* R1/C5 — THREE ref sets on this surface; no Env slot. */}
      <BaseFilesPopup
        open={basePopupOpen}
        onClose={() => setBasePopupOpen(false)}
        slots={SOURCE_PROJECT_REF_SLOTS}
        candidates={candidates}
        selection={eff}
        refs={baseRefs}
        onChange={setBaseRefs}
        onOpenFile={openSelectedFile}
      />
      <AdditionalContextPopup
        open={contextPopupOpen}
        onClose={() => setContextPopupOpen(false)}
        serviceName={serviceName}
        recipePath={recipePath}
        files={additionalContext.files}
        links={additionalContext.links}
        onChange={setAdditionalContext}
        onOpenFile={openSelectedFile}
      />

      {/* Review editor modal */}
      <Modal
        title={editorFile || ''}
        open={editorFile !== null}
        onCancel={() => { setEditorFile(null); setEditorFromPicker(false) }}
        footer={
          <Space>
            <Button onClick={() => { setEditorFile(null); setEditorFromPicker(false) }}>Close</Button>
            <Button type="primary" onClick={saveEditor}>
              {editorFromPicker ? 'Save as .user and use it' : 'Apply edit'}
            </Button>
          </Space>
        }
        width="80%"
      >
        <div style={{ height: '55vh' }}>
          <Editor
            height="100%"
            language={editorFile && (editorFile.endsWith('.yml') || editorFile.endsWith('.yaml')) ? 'yaml'
              : editorFile && editorFile.includes('nginx') ? 'nginx'
              : editorFile && (editorFile.startsWith('.env') || editorFile.endsWith('.env')) ? 'shell' : 'plaintext'}
            theme="vs-dark"
            value={editorContent}
            onChange={(v) => setEditorContent(v || '')}
            options={{ minimap: { enabled: false }, fontSize: 13, wordWrap: 'on', scrollBeyondLastLine: false, automaticLayout: true }}
          />
        </div>
      </Modal>
    </Modal>
  )
}
