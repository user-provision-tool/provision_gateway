import { useSyncExternalStore } from 'react'

/** C16 — the additional generation context is a SHARED per-service store, not
 *  component state: the project tree browser's `add to context` button (on the
 *  Source Projects page and inside the deploy panel's popup) writes the same
 *  store the deploy/generate panels read. Paths are service-root-relative, so
 *  the gateway's context-file validation accepts them from either surface.
 *
 *  Persisted in localStorage as well as memory so the value survives a reload
 *  and is visible to a surface that mounts after the write. */
export interface ServiceContext {
  files: string[]
  links: string[]
}

const EMPTY: ServiceContext = { files: [], links: [] }
const KEY_PREFIX = 'subnet-acl:additional-context:'
const listeners = new Set<() => void>()
const cache = new Map<string, { sig: string; ctx: ServiceContext }>()

function storageKey(service: string): string {
  return `${KEY_PREFIX}${service}`
}

function normalize(value: any): ServiceContext {
  const files: string[] = Array.isArray(value?.files)
    ? (value.files as any[]).filter(x => typeof x === 'string' && x) as string[]
    : []
  const links: string[] = Array.isArray(value?.links)
    ? (value.links as any[]).filter(x => typeof x === 'string' && x) as string[]
    : []
  return { files: [...new Set(files)], links: [...new Set(links)] }
}

function readRaw(service: string): string {
  try {
    return window.localStorage.getItem(storageKey(service)) || ''
  } catch {
    return ''
  }
}

function emit() {
  listeners.forEach(l => l())
}

export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

export function getServiceContext(service: string): ServiceContext {
  if (!service) return EMPTY
  const raw = readRaw(service)
  const cached = cache.get(service)
  if (cached && cached.sig === raw) return cached.ctx
  let ctx: ServiceContext
  if (!raw) {
    ctx = EMPTY
  } else {
    try {
      ctx = normalize(JSON.parse(raw))
    } catch {
      ctx = EMPTY
    }
  }
  cache.set(service, { sig: raw, ctx })
  return ctx
}

export function setServiceContext(service: string, ctx: ServiceContext): void {
  if (!service) return
  const next = normalize(ctx)
  const raw = JSON.stringify(next)
  try {
    window.localStorage.setItem(storageKey(service), raw)
  } catch {
    // A blocked storage still works in memory for this surface.
  }
  cache.set(service, { sig: raw, ctx: next })
  emit()
}

export function addContextFile(service: string, path: string): void {
  const current = getServiceContext(service)
  if (!path || current.files.includes(path)) return
  setServiceContext(service, { ...current, files: [...current.files, path] })
}

export function removeContextFile(service: string, path: string): void {
  const current = getServiceContext(service)
  setServiceContext(service, { ...current, files: current.files.filter(f => f !== path) })
}

export function addContextLink(service: string, url: string): void {
  const current = getServiceContext(service)
  if (!url || current.links.includes(url)) return
  setServiceContext(service, { ...current, links: [...current.links, url] })
}

export function removeContextLink(service: string, url: string): void {
  const current = getServiceContext(service)
  setServiceContext(service, { ...current, links: current.links.filter(l => l !== url) })
}

/** React binding for one service's context. */
export function useServiceContext(service: string): ServiceContext {
  return useSyncExternalStore(
    subscribe,
    () => getServiceContext(service),
    () => getServiceContext(service),
  )
}
