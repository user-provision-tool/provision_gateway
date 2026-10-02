import { useState } from 'react'
import { Alert, Button, message, Input, Space, Typography } from 'antd'

const { Text } = Typography

/**
 * Compose-conflict pause (deploy-panel-selection-and-generation-todo.md §3b).
 *
 * One variable name used at two sites with two DIFFERENT defaults is a compose
 * bug: the name carries two meanings, so no single value satisfies both and the
 * compose is only correct while the variable is unset — a state any operator
 * destroys by following the repo's own `.env.example`. graphiti's `NEO4J_PORT`
 * is the worked example.
 *
 * The guidance below is derived from the `${VAR}` scan itself (site by site, no
 * name lists), pre-filled and EDITABLE; on submit the LLM produces the corrected
 * compose as `docker-compose.fix.yml`.
 */
export default function ComposeFixPrompt({
  jobState, onSubmit, onCancel, cancelLabel = 'Cancel',
}: {
  jobState: any
  onSubmit: (values: Record<string, string>) => void | Promise<void>
  onCancel: () => void
  /** G27: a paused leg is TERMINAL, so the secondary button DISMISSES the view
   *  rather than cancelling a job that has already stopped. */
  cancelLabel?: string
}) {
  const pending = (jobState && jobState.pending_input) || {}
  const conflicts = pending.conflicts || []
  const [guidance, setGuidance] = useState<string>(pending.guidance || '')
  const [submitting, setSubmitting] = useState(false)

  // Mirrors ExternalCredentialPrompt: the submit is AWAITED and its failure is
  // shown. Answering posts a new job, so a rejected request (400 — the server
  // found no pause payload, or the guidance was refused) must keep the prompt
  // open with a reason rather than vanish as an unhandled rejection.
  const submit = async () => {
    setSubmitting(true)
    try {
      await onSubmit({ guidance })
    } catch (err: any) {
      const d = err?.response?.data?.detail
      message.error(typeof d === 'string' ? d : (d?.detail || 'Could not start the fix'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Alert
      type="warning"
      showIcon
      style={{ marginTop: 8 }}
      message="This compose uses one variable name with two different defaults"
      description={
        <Space direction="vertical" style={{ width: '100%' }}>
          <Text style={{ fontSize: 12 }}>
            No single value can satisfy both sites, and the compose is only correct while the
            variable is <Text strong>unset</Text>. The fix is distinct names — the LLM will produce
            the corrected compose as <Text code>docker-compose.fix.yml</Text>.
          </Text>
          {conflicts.length > 0 && (
            <ul style={{ margin: '4px 0 0 16px', padding: 0, fontSize: 12 }}>
              {conflicts.map((c: any) => (
                <li key={c.name}>
                  {/* The defaults are the POINT of this line — the two values
                      that cannot both hold. Rendered as adjacent inline
                      elements they abutted, so the operator read "defaults
                      11112222" and had to guess where one value ended. Comma
                      separated, and keyed by index because the two defaults can
                      legitimately be equal-looking strings. */}
                  <Text code>{c.name}</Text> — defaults{' '}
                  {(c.defaults || []).map((d: string, i: number) => (
                    <span key={`${d}-${i}`}>{i > 0 && ', '}<Text code>{d}</Text></span>
                  ))}
                  {' '}at {(c.sites || []).map((s: any) => `${s.file}`).join(', ')}
                </li>
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
              Fix the compose
            </Button>
            <Button size="small" onClick={onCancel}>{cancelLabel}</Button>
          </Space>
        </Space>
      }
    />
  )
}
