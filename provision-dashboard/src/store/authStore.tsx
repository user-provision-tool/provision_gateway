// Simple auth state management using React Context
import React, { createContext, useContext, useState, useEffect, useCallback } from 'react'
import type { AdminUser } from '../api/auth'
import * as authApi from '../api/auth'

interface AuthState {
  admin: AdminUser | null
  isAuthenticated: boolean
  isLoading: boolean
  needsSetup: boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => void
  setup: (email: string, password: string) => Promise<void>
  refreshAdmin: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [admin, setAdmin] = useState<AdminUser | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [needsSetup, setNeedsSetup] = useState(false)

  const refreshAdmin = useCallback(async () => {
    const token = localStorage.getItem('access_token')
    if (!token) {
      setIsLoading(false)
      return
    }
    try {
      const user = await authApi.getMe()
      setAdmin(user)
      setNeedsSetup(false)
    } catch {
      // Token invalid — check if setup is needed
      try {
        await authApi.checkSetup()
        setNeedsSetup(false)
      } catch {
        setNeedsSetup(true)
      }
      localStorage.removeItem('access_token')
      localStorage.removeItem('refresh_token')
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    refreshAdmin()
  }, [refreshAdmin])

  const login = useCallback(async (email: string, password: string) => {
    const response = await authApi.login({ email, password })
    localStorage.setItem('access_token', response.access_token)
    localStorage.setItem('refresh_token', response.refresh_token)
    setAdmin(response.admin)
    setNeedsSetup(false)
  }, [])

  const logout = useCallback(() => {
    localStorage.removeItem('access_token')
    localStorage.removeItem('refresh_token')
    setAdmin(null)
  }, [])

  const setup = useCallback(async (email: string, password: string) => {
    await authApi.setupAdmin(email, password)
    await login(email, password)
  }, [login])

  return (
    <AuthContext.Provider
      value={{
        admin,
        isAuthenticated: !!admin,
        isLoading,
        needsSetup,
        login,
        logout,
        setup,
        refreshAdmin,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
