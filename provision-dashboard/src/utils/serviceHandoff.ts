// Cross-host service handoff for the portal login.
//
// The ACL gate redirects an unauthenticated browser on a SERVICE host to
// `<portal>/login?next=<service-url>`. The portal login sets a provision_token
// cookie host-scoped to the portal, so simply redirecting back to the service
// would bounce to the login page again forever (cookie never reaches the
// service host). This handoff runs the /go/ flow instead (edge /go/ → gateway
// /api/auth/go → 303 to the service's `_set_token`), which sets the cookie ON
// the service host. Allowed users land on the service; denied users land on
// the portal alert page.

export function getServiceHostFromNext(): { host: string; next: string } | null {
  const next = new URLSearchParams(window.location.search).get('next')
  if (!next) return null
  try {
    const u = new URL(next)
    if (
      (u.protocol === 'http:' || u.protocol === 'https:') &&
      u.host !== window.location.host
    ) {
      return { host: u.host, next }
    }
  } catch {
    // invalid URL — not a service target
  }
  return null
}

/**
 * Complete the /go/ handoff for a service-host `next` parameter.
 *
 * Returns 'service' / 'denied' after triggering a full-page navigation
 * (the caller should keep rendering a placeholder), or 'fallback' when there
 * is no service target or the handoff request failed (caller decides).
 */
export async function startServiceHandoff(): Promise<'service' | 'denied' | 'fallback'> {
  const target = getServiceHostFromNext()
  if (!target) return 'fallback'
  try {
    // Precheck WITHOUT following redirects: /go/ answers 303 (allowed) or
    // 403/404 (denied). We must not follow the chain in fetch — the final
    // hop is a cross-origin service response without CORS headers, which
    // would reject the fetch even though the handoff worked. A top-level
    // navigation has no CORS restrictions and completes the _set_token
    // exchange, landing the provision_token cookie on the SERVICE host.
    const resp = await fetch(`/go/${target.host}`, { redirect: 'manual' })
    if (resp.type === 'opaqueredirect') {
      window.location.href = `/go/${target.host}`
      return 'service'
    }
    // Not a redirect: the gateway denied (403) → ACL alert page.
    window.location.href = `/alert?reason=acl_denied&service=${encodeURIComponent(target.host)}`
    return 'denied'
  } catch {
    return 'fallback'
  }
}
