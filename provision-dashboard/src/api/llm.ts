import client from './client'

export const getLLMConfigs = () => client.get('/llm/configs')
export const getLLMConfig = () => client.get('/llm/config')
export const createLLMConfig = (data: Record<string, any>) => client.post('/llm/configs', data)
export const updateLLMConfig = (data: Record<string, any>) => client.put('/llm/config', data)
export const activateLLMConfig = (id: number) => client.put(`/llm/configs/${id}/activate`)
export const deleteLLMConfig = (id: number) => client.delete(`/llm/configs/${id}`)
export const testLLM = () => client.post('/llm/test')
export const generateLLM = (type: string, context: Record<string, any>) =>
  client.post('/llm/generate', { type, context })
