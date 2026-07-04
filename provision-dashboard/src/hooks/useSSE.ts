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

    const token = localStorage.getItem('access_token')
    // For SSE, we pass token as query param since EventSource doesn't support custom headers
    const urlWithAuth = `${url}${url.includes('?') ? '&' : '?'}token=${token}`
    const es = new EventSource(urlWithAuth)
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
