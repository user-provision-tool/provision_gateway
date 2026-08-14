import { Routes, Route, Navigate } from 'react-router-dom'
import { useAuth } from './hooks/useAuth'
import AppLayout from './components/layout/AppLayout'
import LoginPage from './pages/LoginPage'
import SetupWizard from './pages/SetupWizard'
import DashboardPage from './pages/DashboardPage'
import ServicesPage from './pages/ServicesPage'
import UsersPage from './pages/UsersPage'
import TasksPage from './pages/TasksPage'
import SettingsPage from './pages/SettingsPage'
import AuditPage from './pages/AuditPage'
import UserManagementPage from './pages/UserManagementPage'
import SSLPage from './pages/SSLPage'
import ApiKeysPage from './pages/ApiKeysPage'
import AlertPage from './pages/AlertPage'

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth()

  if (isLoading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '100vh' }}>
        Loading...
      </div>
    )
  }
  if (!isAuthenticated) return <Navigate to="/login" replace />

  return <>{children}</>
}

// Admin-only route guard (Gap 10 / G3): viewers may only reach Services + API Keys.
// The sidebar hides admin nav items, but without this the routes themselves were
// reachable by direct URL (e.g. /dashboard, /audit, /users/manage), leaking admin
// page shells and buttons (and a Reconcile button → 403, Gap 5).
function AdminRoute({ children }: { children: React.ReactNode }) {
  const { admin } = useAuth()
  if (admin?.role !== 'admin') return <Navigate to="/users" replace />
  return <>{children}</>
}

export default function App() {
  const { isAuthenticated, isLoading, needsSetup } = useAuth()

  if (isLoading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '100vh' }}>
        Loading...
      </div>
    )
  }

  return (
    <Routes>
      <Route path="/login" element={
        isAuthenticated ? <Navigate to="/dashboard" replace /> : <LoginPage />
      } />
      <Route path="/setup" element={
        isAuthenticated ? <Navigate to="/dashboard" replace /> : <SetupWizard />
      } />
      <Route path="/" element={
        <ProtectedRoute>
          <AppLayout />
        </ProtectedRoute>
      }>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<AdminRoute><DashboardPage /></AdminRoute>} />
        <Route path="services" element={<AdminRoute><ServicesPage /></AdminRoute>} />
        <Route path="services/:name" element={<AdminRoute><ServicesPage /></AdminRoute>} />
        <Route path="users" element={<UsersPage />} />
        <Route path="users/:name" element={<UsersPage />} />
        <Route path="tasks" element={<AdminRoute><TasksPage /></AdminRoute>} />
        <Route path="settings" element={<AdminRoute><SettingsPage /></AdminRoute>} />
        <Route path="audit" element={<AdminRoute><AuditPage /></AdminRoute>} />
        <Route path="users/manage" element={<AdminRoute><UserManagementPage /></AdminRoute>} />
        <Route path="ssl" element={<AdminRoute><SSLPage /></AdminRoute>} />
        <Route path="api-keys" element={<ApiKeysPage />} />
        <Route path="alert" element={<AlertPage />} />
      </Route>
      <Route path="/alert" element={<AlertPage />} />
    </Routes>
  )
}
