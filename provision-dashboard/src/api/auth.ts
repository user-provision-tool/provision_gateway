import client from './client'

export interface LoginParams {
  email: string
  password: string
}

export interface AdminUser {
  id: number
  email: string
  role: string
  is_active?: boolean
  created_at?: string
  last_login_at?: string | null
  username?: string
  user_type?: string
}

export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  admin?: AdminUser
  user?: AdminUser
  user_type?: string
}

export async function setupAdmin(email: string, password: string) {
  const { data } = await client.post('/auth/setup', { email, password })
  return data
}

export async function login(params: LoginParams): Promise<TokenResponse> {
  const { data } = await client.post('/auth/login', params)
  return data
}

export async function getMe(): Promise<AdminUser> {
  const { data } = await client.get('/auth/me')
  return data
}

export async function changePassword(currentPassword: string, newPassword: string) {
  const { data } = await client.put('/auth/password', {
    current_password: currentPassword,
    new_password: newPassword,
  })
  return data
}

export async function checkSetup(): Promise<{ needs_setup: boolean }> {
  // We check by trying to call setup — if 409, setup already done
  try {
    await client.get('/auth/me')
    return { needs_setup: false }
  } catch {
    return { needs_setup: true }
  }
}
