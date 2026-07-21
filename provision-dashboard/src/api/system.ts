import client from './client'

export const getStatus = () => client.get('/system/status')
export const getStats = (detail = false) => client.get(`/system/stats${detail ? '?detail=true' : ''}`)
export const triggerReconcile = () => client.post('/system/reconcile')
export const getReconcileStatus = () => client.get('/system/reconcile/status')
export const getNginxState = () => client.get('/system/nginx-state')

export const getProxyConfigs = () => client.get('/system/proxy')
export const createProxyConfig = (data: Record<string, any>) => client.post('/system/proxy', data)
export const updateProxyConfig = (id: number, data: Record<string, any>) => client.put(`/system/proxy/${id}`, data)
export const deleteProxyConfig = (id: number) => client.delete(`/system/proxy/${id}`)
export const activateProxyConfig = (id: number) => client.put(`/system/proxy/${id}/activate`)
export const deactivateAllProxy = () => client.post('/system/proxy/deactivate')
export const testAllProxy = () => client.post('/system/proxy/test')

export const getSystemConfig = () => client.get('/system/config')
export const updateSystemConfig = (data: Record<string, any>) => client.put('/system/config', data)

export const getSSLCerts = () => client.get('/system/ssl-certs')
export const uploadSSLCert = (data: Record<string, any>) => client.post('/system/ssl-certs', data)
export const refreshSSLCert = (domain: string) => client.post(`/system/ssl-certs/${domain}/refresh`)
export const deleteSSLCert = (domain: string) => client.delete(`/system/ssl-certs/${domain}`)
