import { useEffect, useState } from 'react'
import { Alert, Button, Divider, Input, Modal, Progress, Space, Spin, Tag, Typography, message } from 'antd'
import Editor from '@monaco-editor/react'
import client from '../../api/client'
import { deriveProfiles } from '../../api/services'
import { useGenerationJob } from '../../hooks/useGenerationJob'
import FileSetSelection, {
  EMPTY_CANDIDATES,
  EMPTY_SELECTION,
  FileSetCandidates,
  FileSetSelectionData,
  FileSetStale,
} from './FileSetSelection'

const { Text } = Typography

const PHASE_FILENAME: Record<string, string> = {
  compose: 'docker-compose.yml',
  nginx: 'nginx.conf',
  env: '.env',
}

interface Props {
  open: boolean
  serviceName: string
  recipePath: string
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
export default function GenerateMissingPanel({ open, serviceName, recipePath, onClose, onChanged }: Props) {
  const [loading, setLoading] = useState(true)
  const [llmActive, setLlmActive] = useState(false)
  const [candidates, setCandidates] = useState<FileSetCandidates>(EMPTY_CANDIDATES)
  const [stale, setStale] = useState<FileSetStale>({})
  const [selection, setSelection] = useState<FileSetSelectionData>(EMPTY_SELECTION)
  const [needsEnv, setNeedsEnv] = useState(true)
  const [prompt, setPrompt] = useState('')
  const { jobId, jobState, polling, startJob, cancelJob, reset } = useGenerationJob()

  // Review/edit gate (design §Generation rules — human gate is final).
  const [reviewFiles, setReviewFiles] = useState<Record<string, string> | null>(null)
  const [validations, setValidations] = useState<Record<string, any>>({})
  const [editorFile, setEditorFile] = useState<string | null>(null)
  const [editorContent, setEditorContent] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!open) return
    setLoading(true)
    setReviewFiles(null)
    setValidations({})
    setPrompt('')
    reset()
    const params: Record<string, string> = {}
    if (recipePath) params.recipe_path = recipePath
    Promise.all([
      client.get(`/services/${serviceName}/file-sets`).then(r => r.data),
      client.get('/llm/config').then(r => r.data).catch(() => ({ is_active: false })),
      client.get(`/services/${serviceName}/check-missing-files`, { params })
        .then(r => r.data).catch(() => ({ needs_env: true })),
    ]).then(([fs, cfg, check]: any[]) => {
      const rp = recipePath || '.'
      const entry = (fs.file_sets || {})[rp] || {}
      const cand = (fs.candidates || {})[rp] || EMPTY_CANDIDATES
      setCandidates(cand)
      setStale((fs.stale || {})[rp] || {})
      setSelection({
        compose: entry.compose || [],
        nginx: entry.nginx || null,
        env: entry.env || [],
        profiles: entry.profiles || [],
      })
      setNeedsEnv(check.needs_env !== false)
      setLlmActive(!!(cfg && (cfg.is_active || cfg.byok_configured)))
    }).catch(() => message.error('Failed to load file selection'))
      .finally(() => setLoading(false))
  }, [open, serviceName, recipePath])

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
    env: selection.env.filter(p => !(stale.env || []).includes(p)),
    profiles: selection.profiles.filter(p => !(stale.profiles || []).includes(p)),
  })

  const startGeneration = async () => {
    if (!llmActive) return
    try {
      await startJob({
        service_name: serviceName,
        recipe_path: recipePath,
        job_type: 'generate_missing',
        prompt,
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
      const { data } = await client.post('/services/save-generated', {
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

  const validationFor = (filename: string) => {
    const phase = Object.keys(PHASE_FILENAME).find(p => PHASE_FILENAME[p] === filename) || ''
    return (validations || {})[phase]
  }

  const jobProgress = (jobState && jobState.phase_total && jobState.phase_total > 1)
    ? Math.round(((jobState.phase_index || 0) + (jobState.status === 'running' ? 0.5 : 0)) / jobState.phase_total * 100)
    : jobState && (jobState.status === 'running' || jobState.status === 'queued') ? 10 : 0

  return (
    <Modal
      title={`Generate Missing Files${serviceName ? `: ${serviceName}${recipePath ? ` @ ${recipePath}` : ''}` : ''}`}
      open={open}
      onCancel={onClose}
      footer={null}
      width={760}
      destroyOnClose
    >
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        {loading ? <div style={{ textAlign: 'center', padding: 24 }}><Spin /></div> : (<>
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
            onChange={setSelection}
          />

          <Divider plain style={{ margin: '8px 0' }}>Generation prompt</Divider>
          <Input.TextArea
            rows={3}
            disabled={!llmActive}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Describe the deployment — REQUIRED when the recipe has no base files at all (empty recipe). You may include doc URLs; the agent fetches them."
          />

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
                          <Button size="small" onClick={() => { setEditorFile(fn); setEditorContent(reviewFiles[fn] || '') }}>Edit</Button>
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
              Generate missing files
            </Button>
          </Space>
        </>)}
      </Space>

      {/* Review editor modal */}
      <Modal
        title={editorFile || ''}
        open={editorFile !== null}
        onCancel={() => setEditorFile(null)}
        footer={
          <Space>
            <Button onClick={() => setEditorFile(null)}>Close</Button>
            <Button type="primary" onClick={() => {
              if (editorFile && reviewFiles) {
                setReviewFiles({ ...reviewFiles, [editorFile]: editorContent })
                setEditorFile(null)
              }
            }}>Apply edit</Button>
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
