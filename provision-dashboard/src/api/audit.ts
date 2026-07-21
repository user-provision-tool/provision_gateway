import client from './client'

export const getAuditLogs = (params?: Record<string, any>) => client.get('/audit', { params })
