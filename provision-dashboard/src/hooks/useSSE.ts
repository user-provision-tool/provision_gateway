import { useEffect, useRef, useState, useCallback } from 'react'

export function useSSE(url: string | null) {
  const [lines, setLines] = useState<string[]>([])
  const [isConnected, setIsConnected] = useState(false)
  const eventSourceRef = useRef<EventSource | null>(null)

  const clearLines = useCallback(() => setLines([]), [])

  useEffect(() => {
    if (!url) {
      setIsConnected(false)
      return
    }

    // v4 §11.2 (N5): auth is the provision_token cookie, auto-sent by
    // EventSource for same-origin requests. No query-param token.
    const es = new EventSource(url)
    eventSourceRef.current = es

    es.onopen = () => setIsConnected(true)
    es.onerror = () => {
      setIsConnected(false)
      es.close()
    }
    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        setLines((prev) => [...prev, data.line || data])
      } catch {
        setLines((prev) => [...prev, event.data])
      }
    }

    return () => {
      es.close()
      setIsConnected(false)
    }
  }, [url])

  return { lines, isConnected, clearLines }
}
