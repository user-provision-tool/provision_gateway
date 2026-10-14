import { useRef, useState } from 'react'
import client from '../api/client'

/**
 * GRILLING G27: a pause is a TERMINAL leg whose payload lives in `result.pause`
 * (`{kind, ask, request}`). The prompts, the review gate and the cards were all
 * written against the legacy PARKED shape, which kept the same payload flat in
 * `pending_input` — and a pause of the new kind matched none of their branches,
 * so a paused leg rendered an empty panel with no way to answer it.
 *
 * Normalised ONCE here, at the single place job state enters the client, so
 * every surface keeps working and the payload still has exactly one home.
 */
const withPause = (d: any) => {
  if (d && (d.status === 'paused' || d.status === 'awaiting_input')) {
    const pause = d.result?.pause
    if (pause && !d.pending_input) {
      return { ...d, pending_input: { kind: pause.kind, ...(pause.ask || {}) } }
    }
  }
  return d
}

/** A leg that has stopped and is waiting for the operator (either shape). */
export const isPaused = (js: any) =>
  !!js && (js.status === 'paused' || js.status === 'awaiting_input')

/**
 * Async LLM generation job client (design §Generation — async execution):
 * create via POST /api/llm/jobs (202), poll GET /api/llm/jobs/{id} every
 * 1.5s, cancel via DELETE. Keeps the latest job state for progress display
 * and the completed result (files + validations) for the review gate.
 */
export function useGenerationJob() {
  const [jobId, setJobId] = useState<number | null>(null)
  // G27: answering a pause creates a NEW job, and callers that WAIT on a job
  // (the deploy auto-run) must follow that hand-off. React state is stale inside
  // such a loop, so the current id is mirrored in a ref that every assignment
  // updates — a loop that polls `jobIdRef.current` follows the resume, while one
  // that captured the id at entry would poll a row that is terminal forever.
  const jobIdRef = useRef<number | null>(null)
  const setJob = (id: number | null) => { jobIdRef.current = id; setJobId(id) }
  const [jobState, setJobState] = useState<any>(null)
  const [polling, setPolling] = useState(false)
  const timerRef = useRef<number | null>(null)

  const stopPolling = () => {
    setPolling(false)
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
  }

  const poll = (id: number) => {
    setPolling(true)
    const tick = async () => {
      try {
        const { data } = await client.get(`/llm/jobs/${id}`)
        setJobState(withPause(data))
        // `paused` IS terminal (G27): the leg ENDED and the answer starts a
        // SEPARATE job, so there is nothing more to poll for here — the popup
        // renders from the pause payload it already has. `awaiting_input` is
        // the legacy parked status and is NOT terminal; polling continues for
        // it so an old row still resolves once its resume runs.
        if (data.status === 'completed' || data.status === 'failed' ||
            data.status === 'cancelled' || data.status === 'paused') {
          stopPolling()
        }
      } catch {
        stopPolling()
      }
    }
    tick()
    if (timerRef.current !== null) window.clearInterval(timerRef.current)
    timerRef.current = window.setInterval(tick, 1500)
  }

  const submitInput = async (values: Record<string, string>) => {
    // G27: ANSWERING A PAUSE CREATES A NEW JOB. `POST /llm/jobs/{id}/input` —
    // which rewrote the parked row and re-ran it — is GONE, so this posts the
    // pause payload back to the ordinary create entry point under `resume`.
    // The carried `request` inside it IS the next leg's request (the identity —
    // user, label, proxy — rides along with it), and `values` is the operator's
    // input: the edited guidance, or the credential values.
    const pause = jobState?.result?.pause
    if (!pause) {
      throw new Error('this job is not waiting for input — nothing to answer')
    }
    // Errors are surfaced to the caller: a 400 (missing required value) must
    // keep the popup open rather than looking like a successful submit.
    //
    // `parent_job_id` names the leg this answer CONSUMES: the server cancels it
    // once the new leg exists (GRILLING G34 — an answered pause keeps its
    // `paused` status forever, which is a paused object with nothing owning it,
    // and a paused record is never swept).
    const { data } = await client.post('/llm/jobs', { resume: pause, values, parent_job_id: jobId })
    // Follow the NEXT leg. Without this the caller kept polling the finished
    // leg, whose `result.files` are absent by construction, and rendered the
    // review gate EMPTY — an AntD list reading "No data" behind a "Save fixed
    // files" button with nothing to save — while the real output sat in a job
    // nothing watched. Found live in Round 32, before this handoff existed.
    const next = data?.job_id
    if (next != null) {
      setJob(next)
      setJobState({ status: 'queued', progress: 'queued', phase: '', phase_index: 0, phase_total: 1 })
      poll(next)
    }
  }

  const startJob = async (body: any): Promise<number | null> => {
    try {
      const { data } = await client.post('/llm/jobs', body)
      setJob(data.job_id)
      setJobState({ status: 'queued', progress: 'queued', phase: '', phase_index: 0, phase_total: 1 })
      poll(data.job_id)
      return data.job_id as number
    } catch (err: any) {
      stopPolling()
      // R3-F48 — never let an API ERROR BODY reach a render.
      //
      // A 409 from a paused job carries `{error, detail}`. A caller that stored
      // that object and rendered it killed React (#31, "objects are not valid as
      // a React child"), which blanked the deploy panel — INCLUDING the pause
      // dialog that lived in the same tree, so the paused job became impossible
      // to answer and every later deploy for that service was refused with the
      // same 409. Measured 2026-10-10. The message is flattened here, at the one
      // boundary every caller passes through, so no caller can undo it.
      const d = err?.response?.data
      const msg =
        (typeof d?.detail === 'string' && d.detail) ||
        (typeof d?.error === 'string' && d.error) ||
        (typeof d === 'string' && d) ||
        err?.message ||
        'Generation failed'
      const normalised: any = new Error(msg)
      // The status stays reachable for callers that branch on it.
      normalised.response = err?.response
      throw normalised
    }
  }

  const cancelJob = async () => {
    if (jobId === null) return
    // ADOPT what the server answers rather than asserting 'cancelled'. The
    // endpoint reports the status the row ACTUALLY has afterwards
    // (`{"job_id", "status"}`), which is what keeps cancel from ever claiming
    // success with no job behind it (T14's rule): the old optimistic write told
    // the operator a job was cancelled while the row still said otherwise.
    //
    // The cancellable set is the manager's `CANCELLABLE_STATUSES` —
    // `queued|running|awaiting_input|paused`. `paused` belongs there (GRILLING
    // G34): G27 made the pause a terminal LEG, but the job still exists, so the
    // DELETE ends it instead of no-op'ing.
    const { data } = await client.delete(`/llm/jobs/${jobId}`)
    const status = data?.status
    if (status) {
      setJobState((prev: any) => ({ ...(prev || {}), status, progress: status }))
    }
    // Keep polling unless the job really is over.
    if (['completed', 'failed', 'cancelled', 'paused'].includes(status)) stopPolling()
  }

  const reset = () => {
    stopPolling()
    setJob(null)
    setJobState(null)
  }

  /**
   * Re-attach to a job that is still running server-side.
   *
   * A generation job outlives the modal that started it: it runs in the gateway,
   * not the browser. If the page reloads while the job is paused for input, the
   * modal is gone and the job is orphaned — nothing polls it, and (see the
   * generation-job cancel gap in the task doc) nothing in the dashboard can even
   * cancel it, because TasksPage lists only the -api's tasks. Re-attaching from a
   * persisted id is what makes such a job reachable again.
   */
  const resumeJob = (id: number) => {
    setJob(id)
    setJobState({ status: 'queued', progress: 'resuming…', phase: '', phase_index: 0, phase_total: 1 })
    poll(id)
  }

  return { jobId, jobIdRef, jobState, polling, startJob, cancelJob, submitInput, resumeJob, reset }
}
