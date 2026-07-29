import client from './client'

export const createServiceGit = (data: Record<string, any>) => client.post('/services', data)
