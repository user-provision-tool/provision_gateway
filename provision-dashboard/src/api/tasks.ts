import client from './client'

export const getTasks = () => client.get('/tasks')
export const getTask = (taskId: string) => client.get(`/tasks/${taskId}`)
export const cancelTask = (taskId: string) => client.delete(`/tasks/${taskId}`)
export const getTaskLogURL = (taskId: string, tail?: number) =>
  `/api/tasks/${taskId}/log${tail ? `?tail=${tail}` : ''}`
