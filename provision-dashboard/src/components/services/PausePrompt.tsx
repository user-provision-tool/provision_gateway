import ComposeFixPrompt from './ComposeFixPrompt'
import ExternalCredentialPrompt from './ExternalCredentialPrompt'
import ValidationFixPrompt from './ValidationFixPrompt'

/**
 * ONE home for "which prompt answers which pause kind".
 *
 * Both surfaces that can answer a pause (the deploy panel's progress popup and
 * the standalone Generate panel) used to spell the choice out themselves as
 * `kind === 'compose_fix' ? ComposeFix : ExternalCredential` — so a NEW kind
 * silently landed on the credential prompt, which renders the wrong fields and
 * offers a submit that cannot answer it. That is the same dead-end shape G27
 * already cost a round (a paused leg rendering an empty panel). The dispatch
 * lives here now, so adding a kind means adding a case in ONE place.
 *
 * The kinds today:
 *  - `compose_fix`            — one variable name with two different defaults.
 *  - `input`                  — operator-supplied credential values.
 *  - `validate_ports_fix`     — the serving-ports artifact failed its check.
 *  - `validate_conf_fix`      — the nginx conf does not agree with the ports file.
 * The last two carry their own kind so the prompt names the right file (R1.3).
 */
export default function PausePrompt({
  jobState, onSubmit, onCancel, cancelLabel = 'Cancel',
}: {
  jobState: any
  onSubmit: (values: Record<string, string>) => Promise<void>
  onCancel: () => void
  cancelLabel?: string
}) {
  const kind = ((jobState && jobState.pending_input) || {}).kind
  const shared = { jobState, onSubmit, onCancel, cancelLabel }
  if (kind === 'compose_fix') return <ComposeFixPrompt {...shared} />
  if (kind === 'validate_ports_fix' || kind === 'validate_conf_fix') {
    return <ValidationFixPrompt {...shared} />
  }
  return <ExternalCredentialPrompt {...shared} />
}
