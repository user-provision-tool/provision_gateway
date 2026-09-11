import client from './client'

export const getUsers = () => client.get('/services')
export const getUser = (name: string) => client.get(`/services/${name}`)
export const deploy = (data: Record<string, any>) => client.post('/services/deploy', data)
export const cloneAll = (sourceUser: string, targetUser: string, domain: string, passwd: string, volumeOverride?: string) =>
  client.post('/services/clone', { source_user: sourceUser, target_user: targetUser, domain, passwd, volume_base_override: volumeOverride })

export const rebuild = (user: string, service: string, label: string, noCache = true, buildArgs?: Record<string, string>) =>
  client.post(`/services/${user}/${service}/${label}/rebuild`, { no_cache: noCache, build_args: buildArgs || {} })
export const startService = (user: string, service: string, label: string) =>
  client.post(`/services/${user}/${service}/${label}/up`)
export const stopService = (user: string, service: string, label: string) =>
  client.post(`/services/${user}/${service}/${label}/down`)
export const removeService = (user: string, service: string, label: string) =>
  client.delete(`/services/${user}/${service}/${label}`)
export const changePassword = (user: string, service: string, label: string, passwd: string) =>
  client.put(`/services/${user}/${service}/${label}/password`, { passwd })

export const getServiceURL = (user: string, service: string, label: string) =>
  client.get(`/services/${user}/${service}/${label}/url`)
export const testCurl = (user: string, service: string, label: string, includeAuth = false, followRedirect = true) =>
  client.post(`/services/${user}/${service}/${label}/test-curl`, { include_auth: includeAuth, follow_redirect: followRedirect })
export const getContainerLogs = (user: string, service: string, label: string, container: string, tail?: number) =>
  client.get(`/services/${user}/${service}/${label}/containers/${container}/logs${tail ? `?tail=${tail}` : ''}`)

// Per-user deployment files are recipe-scoped, keyed by (recipe_path, filename)
// — the old convention-based list/edit endpoints were retired (GAP-18).
export const getPerUserFile = (user: string, service: string, label: string, recipePath: string, filename: string) =>
  client.get(`/services/${user}/${service}/${label}/per-user-file`, { params: { recipe_path: recipePath, filename } })
export const savePerUserFile = (user: string, service: string, label: string, recipePath: string, filename: string, content: string) =>
  client.put(`/services/${user}/${service}/${label}/per-user-file`, { recipe_path: recipePath, filename, content })

export const getRegistrationTime = (user: string, service: string, label: string) =>
  client.get(`/services/${user}/${service}/${label}/registration-time`)
