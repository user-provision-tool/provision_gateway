import client from './client'

export const getUsers = () => client.get('/users')
export const getUser = (name: string) => client.get(`/users/${name}`)
export const deploy = (data: Record<string, any>) => client.post('/users/deploy', data)
export const cloneAll = (sourceUser: string, targetUser: string, domain: string, passwd: string, volumeOverride?: string) =>
  client.post('/users/clone', { source_user: sourceUser, target_user: targetUser, domain, passwd, volume_base_override: volumeOverride })

export const rebuild = (user: string, service: string, label: string, noCache = true, buildArgs?: Record<string, string>) =>
  client.post(`/users/${user}/${service}/${label}/rebuild`, { no_cache: noCache, build_args: buildArgs || {} })
export const startService = (user: string, service: string, label: string) =>
  client.post(`/users/${user}/${service}/${label}/up`)
export const stopService = (user: string, service: string, label: string) =>
  client.post(`/users/${user}/${service}/${label}/down`)
export const removeService = (user: string, service: string, label: string) =>
  client.delete(`/users/${user}/${service}/${label}`)
export const changePassword = (user: string, service: string, label: string, passwd: string) =>
  client.put(`/users/${user}/${service}/${label}/password`, { passwd })

export const getServiceURL = (user: string, service: string, label: string) =>
  client.get(`/users/${user}/${service}/${label}/url`)
export const testCurl = (user: string, service: string, label: string, includeAuth = false, followRedirect = true) =>
  client.post(`/users/${user}/${service}/${label}/test-curl`, { include_auth: includeAuth, follow_redirect: followRedirect })
export const getContainerLogs = (user: string, service: string, label: string, container: string, tail?: number) =>
  client.get(`/users/${user}/${service}/${label}/containers/${container}/logs${tail ? `?tail=${tail}` : ''}`)

export const getDeploymentFiles = (user: string, service: string, label: string) =>
  client.get(`/users/${user}/${service}/${label}/deployment-files`)
export const getDeploymentFile = (user: string, service: string, label: string, fileType: string) =>
  client.get(`/users/${user}/${service}/${label}/deployment-files/${fileType}`)
export const updateDeploymentFile = (user: string, service: string, label: string, fileType: string, content: string) =>
  client.put(`/users/${user}/${service}/${label}/deployment-files/${fileType}`, { content })

export const getRegistrationTime = (user: string, service: string, label: string) =>
  client.get(`/users/${user}/${service}/${label}/registration-time`)
