import { useRef, useState } from 'react'
import client from '../api/client'

/**
 * Async LLM generation job client (design §Generation — async execution):
 * create via POST /api/llm/jobs (202), poll GET /api/llm/jobs/{id} every
 * 1.5s, cancel via DELETE. Keeps the latest job state for progress display
 * and the completed result (files + validations) for the review gate.
 */
export function useGenerationJob() {
  const [jobId, setJobId] = useState<number | null>(null)
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
        setJobState(data)
        if (data.status === 'completed' || data.status === 'failed' || data.status === 'cancelled') {
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

  const startJob = async (body: any): Promise<number | null> => {
    try {
      const { data } = await client.post('/llm/jobs', body)
      setJobId(data.job_id)
      setJobState({ status: 'queued', progress: 'queued', phase: '', phase_index: 0, phase_total: 1 })
      poll(data.job_id)
      return data.job_id as number
    } catch (err: any) {
      stopPolling()
      throw err
    }
  }

  const cancelJob = async () => {
    if (jobId === null) return
    try {
      await client.delete(`/llm/jobs/${jobId}`)
      setJobState((prev: any) => ({ ...(prev || {}), status: 'cancelled', progress: 'cancelled' }))
    } finally {
      stopPolling()
    }
  }

  const reset = () => {
    stopPolling()
    setJobId(null)
    setJobState(null)
  }

  return { jobId, jobState, polling, startJob, cancelJob, reset }
}
