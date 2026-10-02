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
      // The -api streams line frames as `{"line": "..."}` — and a BLANK line in
      // the log arrives as `{"line": ""}`. `data.line || data` fell through on
      // that empty string and pushed the whole OBJECT into the line list, which
      // React refuses to render ("Objects are not valid as a React child",
      // error #31) and which took the ENTIRE log drawer down with it. The
      // presence of the key is what distinguishes a line frame from a payload,
      // never its truthiness.
      let text = event.data
      try {
        const data = JSON.parse(event.data)
        if (data && typeof data === 'object' && 'line' in data) {
          text = String(data.line ?? '')
        }
      } catch { /* not JSON — a raw line, kept as-is */ }
      setLines((prev) => [...prev, text])
    }

    return () => {
      es.close()
      setIsConnected(false)
    }
  }, [url])

  return { lines, isConnected, clearLines }
}
