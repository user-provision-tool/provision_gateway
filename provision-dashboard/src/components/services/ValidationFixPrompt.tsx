import { useState } from 'react'
import { Alert, Button, message, Input, Space, Typography } from 'antd'

const { Text } = Typography

/**
 * The two VALIDATION pauses — `validate_ports_fix` and `validate_conf_fix`
 * (task doc R1.3 / T14, GRILLING G43).
 *
 * Either check can fail on the deploy-scoped path, and R1.3 makes what happens
 * next depend on whether a pause is possible: the deploy path PAUSES — a pause
 * IS a session, and one exists there — while the standalone Generate panel, with
 * no user and no label to hang a session on, RAISES. The reason is not
 * decoration: a pause keeps the run and the artifacts it has already produced,
 * so the retry does not re-pay for the compose and the ports file the run already
 * generated.
 *
 * Each check carries its OWN kind so this prompt names the right file — the
 * serving-ports artifact for `validate_ports`, the nginx conf for
 * `validate_conf`. The server has already carried that file's slot UNSELECTED in
 * the pause's `request`, so confirming regenerates the artifact the check
 * rejected instead of judging the same file again.
 */
export default function ValidationFixPrompt({
  jobState, onSubmit, onCancel, cancelLabel = 'Cancel',
}: {
  jobState: any
  onSubmit: (values: Record<string, string>) => Promise<void>
  onCancel: () => void
  /** G27: a paused leg is TERMINAL, so the secondary button DISMISSES the view
   *  rather than cancelling a job that has already stopped. */
  cancelLabel?: string
}) {
  const pending = (jobState && jobState.pending_input) || {}
  const file: string = pending.file || ''
  const errors: string[] = pending.errors || []
  const [guidance, setGuidance] = useState<string>(pending.guidance || '')
  const [submitting, setSubmitting] = useState(false)

  // Mirrors ComposeFixPrompt: the submit is AWAITED and its failure is shown.
  // Answering posts a new job, so a rejected request must keep the prompt open
  // with a reason rather than vanish as an unhandled rejection.
  const submit = async () => {
    setSubmitting(true)
    try {
      await onSubmit({ guidance })
    } catch (err: any) {
      const d = err?.response?.data?.detail
      message.error(typeof d === 'string' ? d : (d?.detail || 'Could not resume the run'))
    } finally {
      setSubmitting(false)
    }
  }

  const checkLabel = pending.check === 'validate_ports'
    ? 'the serving-ports file'
    : 'the nginx conf'

  return (
    <Alert
      type="warning"
      showIcon
      style={{ marginTop: 8 }}
      message={`${file || checkLabel} did not pass the ${pending.check || 'validation'} check`}
      description={
        <Space direction="vertical" style={{ width: '100%' }}>
          <Text style={{ fontSize: 12 }}>
            The run has already produced everything up to this point, so confirming regenerates{' '}
            <Text code>{file || checkLabel}</Text> and resumes from here — the artifacts already
            built are kept.
          </Text>
          {errors.length > 0 && (
            <ul style={{ margin: '4px 0 0 16px', padding: 0, fontSize: 12 }}>
              {errors.map((e: string, i: number) => (
                <li key={`${i}-${e}`}><Text type="danger">{e}</Text></li>
              ))}
            </ul>
          )}
          <div>
            <Text strong style={{ fontSize: 12 }}>Guidance sent to the model (editable)</Text>
            <Input.TextArea
              rows={4}
              value={guidance}
              onChange={(e) => setGuidance(e.target.value)}
              style={{ marginTop: 4 }}
            />
          </div>
          <Space>
            <Button
              type="primary"
              size="small"
              loading={submitting}
              disabled={!guidance.trim()}
              onClick={submit}
            >
              Regenerate and resume
            </Button>
            <Button size="small" onClick={onCancel}>{cancelLabel}</Button>
          </Space>
        </Space>
      }
    />
  )
}
