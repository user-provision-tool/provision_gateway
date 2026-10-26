import { useState } from 'react'
import { Button, Input, Space, Typography, message } from 'antd'

const { Text } = Typography

/**
 * External-credential pause prompt (task doc `env-external-vars-classify-and-pause-todo.md` §2).
 *
 * Turn 1 classifies a variable as a third-party credential when the platform cannot invent its
 * value: a random string for an externally-issued key looks configured and fails at first use.
 * The job then parks in `awaiting_input` BEFORE the env file is written, and it is blocking by
 * design (GRILLING G7) — the only exits are supplying the values or cancelling the job.
 *
 * Shared deliberately: a job can be started from the Deploy panel or from the standalone
 * generation panel, and a pause must be answerable from whichever surface started it. Rendered
 * in DeployForm (both the inline progress area and the "Deploying…" popup) and in
 * GenerateMissingPanel.
 */
export default function ExternalCredentialPrompt({
  jobState,
  onSubmit,
  onCancel,
  cancelLabel = 'Cancel',
}: {
  jobState: any
  onSubmit: (values: Record<string, string>) => Promise<void>
  onCancel: () => void
  /** G27: a paused leg is TERMINAL — this button DISMISSES, it does not cancel
   *  a job that has already stopped. */
  cancelLabel?: string
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [submitting, setSubmitting] = useState(false)

  const vars: string[] = jobState?.pending_input?.vars || []
  const missing = vars.some(v => !(values[v] || '').trim())

  const submit = async () => {
    setSubmitting(true)
    try {
      await onSubmit(values)
      setValues({})
    } catch (err: any) {
      // A 400 (missing required value) must keep the prompt open rather than look like success.
      message.error(err?.response?.data?.detail || 'Failed to submit credentials')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div style={{ marginTop: 8, padding: 8, border: '1px solid #faad14', borderRadius: 4, background: '#fffbe6' }}>
      <Text strong style={{ fontSize: 12 }}>
        Waiting for external credential(s) — supply them to continue
      </Text>
      <div style={{ fontSize: 11, color: '#8c6d1f', marginTop: 2 }}>
        The deployment is paused before the env file is written. Values are written into it like any
        other variable, so a later deploy of this instance that reuses the file will not ask again.
      </div>
      {vars.map(v => (
        <div key={v} style={{ marginTop: 6 }}>
          <Text code style={{ fontSize: 12 }}>{v}</Text>
          <Input.Password
            size="small"
            style={{ marginTop: 2 }}
            name={`credential_${v.toLowerCase()}`}
            autoComplete="new-password"
            data-lpignore="true" data-1p-ignore="true" data-bwignore="true"
            placeholder="value issued by the third party"
            value={values[v] || ''}
            onChange={e => setValues(prev => ({ ...prev, [v]: e.target.value }))}
          />
        </div>
      ))}
      <Space style={{ marginTop: 8 }}>
        <Button size="small" type="primary" loading={submitting} disabled={missing} onClick={submit}>
          Submit
        </Button>
        <Button size="small" danger={cancelLabel === 'Cancel'} onClick={onCancel}>{cancelLabel}</Button>
      </Space>
    </div>
  )
}
