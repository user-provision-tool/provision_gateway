import { useState, useEffect } from 'react'
import {
  Typography, Card, Table, Tag, Space, Button, message, Spin, Empty, Modal,
  Form, Input, Select, Collapse, Switch,
} from 'antd'
import {
  UserAddOutlined, CheckOutlined, CloseOutlined, ReloadOutlined,
  SettingOutlined, KeyOutlined,
} from '@ant-design/icons'
import client from '../api/client'
import { useAuth } from '../store/authStore'

const { Title, Text } = Typography

/**
 * User management (G78).
 *
 * One row per account, including reserved tombstones. The two state controls
 * are deliberate:
 *
 * - `Active` deactivates/activates the account. Deactivation inactivates every
 *   grant row where the account is grantee OR shared; activation does NOT
 *   restore them.
 * The public flag is NOT a switch on this page. It is chosen once, at account
 * registration, and only for a `special` functional user; the list shows a
 * green `Public` tag after that username. Demoting a special user to viewer
 * clears the flag server-side.
 *
 * The grant dialog is a tag set, not a form. A tag with a row is purple when
 * active and grey when inactive; a public shared user is green. Clicking a tag
 * calls the per-row API immediately — that click is the manual re-approval
 * G78 requires.
 */
export default function UserManagementPage() {
  const [users, setUsers] = useState<any[]>([])
  const [candidates, setCandidates] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [addOpen, setAddOpen] = useState(false)
  const [addLoading, setAddLoading] = useState(false)
  const [form] = Form.useForm()
  const roleValue = Form.useWatch('role', form)
  const { admin: currentUser } = useAuth()
  const currentUserId = (currentUser as any)?.id

  // Typed-confirmation state for the two destructive account actions — the
  // same random-token pattern as the "Delete service — confirm" popup.
  const [roleTarget, setRoleTarget] = useState<any>(null)
  const [roleNext, setRoleNext] = useState<'viewer' | 'admin'>('admin')
  const [roleToken, setRoleToken] = useState('')
  const [roleInput, setRoleInput] = useState('')
  const [roleLoading, setRoleLoading] = useState(false)
  const [delTarget, setDelTarget] = useState<any>(null)
  const [delToken, setDelToken] = useState('')
  const [delInput, setDelInput] = useState('')
  const [delLoading, setDelLoading] = useState(false)
  const newToken = () => Math.random().toString(36).slice(2, 8)
  const isSelf = (user: any) => String(user?.id) === String(currentUserId)

  const [specialUsersModalOpen, setSpecialUsersModalOpen] = useState(false)
  const [specialUsersTarget, setSpecialUsersTarget] = useState<any>(null)
  const [grantLoading, setGrantLoading] = useState(false)

  useEffect(() => { loadUsers() }, [])

  const loadUsers = async () => {
    setLoading(true)
    try {
      const { data } = await client.get('/auth/users')
      setUsers(data.users || [])
      setCandidates(data.candidates || [])
    } catch (err: any) {
      message.error('Failed to load users')
    } finally { setLoading(false) }
  }

  // Silent refresh after a mutation: update the table in place — no spinner,
  // no flash, no page reload. A failed silent refresh keeps the current table.
  const refreshUsersSilently = async () => {
    try {
      const { data } = await client.get('/auth/users')
      setUsers(data.users || [])
      setCandidates(data.candidates || [])
    } catch (err) {
      console.warn('silent user refresh failed', err)
    }
  }

  const errorText = (err: any): string => {
    const detail = err?.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object') {
      const base = detail.message || detail.detail || 'Request failed'
      const failed = detail.failed_services
      if (Array.isArray(failed) && failed.length) {
        return `${base} — failed: ${failed.map((f: any) =>
          `${f.service_name}/${f.label}`).join(', ')}`
      }
      return base
    }
    return err?.message || 'Request failed'
  }

  const handleAdd = async (values: any) => {
    setAddLoading(true)
    try {
      if (values.role === 'special') {
        await client.post('/auth/users/register-special', {
          username: values.username,
          password: values.password,
          is_public_shared: !!values.is_public_shared,
        })
      } else {
        // Public signup is viewer-only and closed until the bootstrap admin.
        await client.post('/auth/users/register', {
          username: values.username,
          password: values.password,
        })
      }
      message.success('User registered')
      setAddOpen(false)
      form.resetFields()
      refreshUsersSilently()
    } catch (err: any) { message.error(errorText(err)) }
    finally { setAddLoading(false) }
  }

  const openRoleChange = (user: any) => {
    if (isSelf(user)) {
      message.error('You cannot change your own role')
      return
    }
    setRoleNext(user.role === 'admin' ? 'viewer' : 'admin')
    setRoleToken(newToken())
    setRoleInput('')
    setRoleTarget(user)
  }

  const confirmRoleChange = async () => {
    if (!roleTarget || roleInput !== roleToken) return
    setRoleLoading(true)
    try {
      await client.put(`/auth/users/${roleTarget.id}`, { role: roleNext })
      message.success(`${roleTarget.username} is now ${roleNext}`)
      setUsers(prev => prev.map(u =>
        u.id === roleTarget.id ? { ...u, role: roleNext } : u))
      setRoleTarget(null)
      setRoleInput('')
      refreshUsersSilently()
    } catch (err: any) { message.error(errorText(err)) }
    finally { setRoleLoading(false) }
  }

  const handleApprove = async (id: string) => {
    try {
      await client.put(`/auth/users/${id}/approve`)
      message.success('Approved')
      setUsers(prev => prev.map(u => u.id === id ? { ...u, is_approved: true } : u))
      refreshUsersSilently()
    }
    catch (err: any) { message.error(errorText(err)) }
  }

  const toggleActive = async (user: any, checked: boolean) => {
    try {
      await client.put(`/auth/users/${user.id}`, { is_active: checked })
      message.success(checked
        ? `${user.username} activated — grants stay inactive until re-approved`
        : `${user.username} deactivated — all related grants are now inactive`)
      setUsers(prev => prev.map(u =>
        u.id === user.id ? { ...u, is_active: checked } : u))
      refreshUsersSilently()
    } catch (err: any) { message.error(errorText(err)) }
  }

  const openDelete = (user: any) => {
    if (isSelf(user)) {
      message.error('You cannot delete your own account')
      return
    }
    setDelToken(newToken())
    setDelInput('')
    setDelTarget(user)
  }

  const confirmDelete = async () => {
    if (!delTarget || delInput !== delToken) return
    setDelLoading(true)
    try {
      await client.delete(`/auth/users/${delTarget.id}`)
      message.success('Account deleted; its services are stopped and orphaned')
      setUsers(prev => prev.map(u =>
        u.id === delTarget.id ? { ...u, is_deleted: true, is_active: false } : u))
      setDelTarget(null)
      setDelInput('')
      refreshUsersSilently()
    } catch (err: any) {
      Modal.error({ title: 'Deletion aborted', content: errorText(err) })
    } finally { setDelLoading(false) }
  }

  const [pwdTarget, setPwdTarget] = useState<any>(null)
  const [pwdLoading, setPwdLoading] = useState(false)
  const [pwdForm] = Form.useForm()

  const resetPassword = async (values: any) => {
    if (!pwdTarget) return
    setPwdLoading(true)
    try {
      await client.put(`/auth/users/${pwdTarget.id}/password`, { password: values.password })
      message.success(`Password reset for ${pwdTarget.username}`)
      setPwdTarget(null)
      pwdForm.resetFields()
    } catch (err: any) { message.error(errorText(err)) }
    finally { setPwdLoading(false) }
  }

  const openSpecialUsers = (user: any) => {
    setSpecialUsersTarget(user)
    setSpecialUsersModalOpen(true)
  }

  const replaceTargetGrants = (targetId: string, nextGrants: any[]) => {
    setSpecialUsersTarget((prev: any) =>
      prev && prev.id === targetId ? { ...prev, grants: nextGrants } : prev)
    setUsers((prev) => prev.map((u) =>
      u.id === targetId ? { ...u, grants: nextGrants } : u))
  }

  const toggleGrant = async (shared: any, currentState?: string) => {
    if (!specialUsersTarget) return
    const active = currentState !== 'active'
    setGrantLoading(true)
    try {
      const { data } = await client.put(
        `/auth/users/${specialUsersTarget.id}/grants`,
        { shared_user_id: shared.id, active },
      )
      const grants = [...(specialUsersTarget.grants || [])]
      const idx = grants.findIndex((g: any) => g.shared_user_id === shared.id)
      if (data?.grant) {
        const grant = { ...data.grant, is_public_shared: !!shared.is_public_shared }
        if (idx >= 0) grants[idx] = grant
        else grants.push(grant)
      } else if (idx >= 0) {
        grants.splice(idx, 1)
      }
      replaceTargetGrants(specialUsersTarget.id, grants)
      message.success(active ? 'Grant activated' : 'Grant deactivated')
    } catch (err: any) { message.error(errorText(err)) }
    finally { setGrantLoading(false) }
  }

  const grantState = (sharedId: string): string | undefined =>
    (specialUsersTarget?.grants || []).find((g: any) => g.shared_user_id === sharedId)?.state

  const modalTags = (() => {
    if (!specialUsersTarget) return []
    const rows = candidates.filter((c: any) => c.id !== specialUsersTarget.id)
    for (const g of specialUsersTarget.grants || []) {
      if (!rows.find((c: any) => c.id === g.shared_user_id)) {
        rows.push({
          id: g.shared_user_id,
          username: g.shared_username,
          is_active: true,
          is_public_shared: !!g.is_public_shared,
        })
      }
    }
    return rows
  })()

  const columns = [
    {
      title: 'Username', dataIndex: 'username', key: 'username',
      render: (t: string, r: any) => (
        <Space size={4}>
          <strong>{t}</strong>
          {(r.is_public_shared || r.was_public) && <Tag color="green">Public</Tag>}
          {r.is_deleted && <Tag color="red">Deleted</Tag>}
        </Space>
      ),
    },
    {
      title: 'Role', dataIndex: 'role', key: 'role',
      render: (r: string) => {
        const color = r === 'admin' ? 'red' : r === 'special' ? 'purple' : 'blue'
        return <Tag color={color}>{r}</Tag>
      },
    },
    {
      title: 'Status', key: 'status',
      render: (_: any, r: any) => {
        if (r.is_deleted) return <Tag color="red">Deleted</Tag>
        if (!r.is_active) return <Tag color="default">Inactive</Tag>
        return r.is_approved
          ? <Tag color="green">Approved</Tag>
          : <Tag color="gold">Pending</Tag>
      },
    },
    {
      title: 'Active', key: 'active',
      render: (_: any, r: any) => (
        <Switch
          size="small"
          checked={!!r.is_active}
          disabled={r.is_deleted}
          onChange={(checked) => toggleActive(r, checked)}
        />
      ),
    },
    {
      title: 'Allowed Shared Users', key: 'grants',
      render: (_: any, r: any) => {
        const grants = r.grants || []
        if (grants.length === 0) return <Text type="secondary">none</Text>
        return (
          <Space size={2} wrap>
            {grants.map((g: any) => (
              <Tag
                key={g.shared_user_id}
                color={g.state === 'active'
                  ? (g.is_public_shared ? 'green' : 'purple') : 'default'}
                style={{ opacity: g.state === 'active' ? 1 : 0.45 }}
              >
                {g.shared_username}{g.is_public_shared ? ' · Public' : ''}{g.state === 'active' ? '' : ' (off)'}
              </Tag>
            ))}
          </Space>
        )
      },
    },
    {
      title: 'Actions', key: 'actions',
      render: (_: any, r: any) => (
        <Space>
          <Button
            size="small"
            icon={<SettingOutlined />}
            disabled={r.is_deleted}
            onClick={() => openSpecialUsers(r)}
            title="Set allowed shared functional users"
          >Shared</Button>
          {!r.is_approved && !r.is_deleted && (
            <Button size="small" type="primary" icon={<CheckOutlined />}
              onClick={() => handleApprove(r.id)}>Approve</Button>
          )}
          {r.role !== 'special' && !r.is_deleted && (
            <Button size="small" onClick={() => openRoleChange(r)}
              disabled={isSelf(r)}
              title={isSelf(r) ? 'You cannot change your own role'
                : (r.role === 'admin' ? 'Demote to viewer' : 'Promote to admin')}>
              {r.role === 'admin' ? 'Viewer' : 'Admin'}
            </Button>
          )}
          <Button size="small" icon={<KeyOutlined />} disabled={r.is_deleted}
            onClick={() => { pwdForm.resetFields(); setPwdTarget(r) }}
            title="Reset password">Pwd</Button>
          <Button size="small" danger icon={<CloseOutlined />} disabled={r.is_deleted || isSelf(r)}
            onClick={() => openDelete(r)}
            title={isSelf(r) ? 'You cannot delete your own account' : 'Soft-delete the account'} />
        </Space>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>User Management</Title>
        <Space>
          <Button type="primary" icon={<UserAddOutlined />} onClick={() => setAddOpen(true)}>Register User</Button>
          <Button icon={<ReloadOutlined />} onClick={loadUsers}>Refresh</Button>
        </Space>
      </div>

      <Collapse style={{ marginBottom: 16 }} items={[{
        key: 'shared-users-config',
        label: <Space><SettingOutlined /><Text strong>Shared Functional Users</Text></Space>,
        children: <div>
          <Text type="secondary">
            Shared functional users are registered as regular users with role "Special".
            A grant is a row in <code>acl_grants</code>; deactivating either account makes
            every related row inactive, and activation never restores it automatically —
            click the greyed tag to re-approve.
          </Text>
          <div style={{ marginTop: 12 }}>
            <Text strong>Eligible shared accounts: </Text>
            {candidates.length > 0
              ? <Space size={4} wrap>{candidates.map((u: any) => (
                  <Tag key={u.id}
                    color={u.is_active ? (u.is_public_shared ? 'green' : 'purple') : 'default'}
                    style={{ opacity: u.is_active ? 1 : 0.45 }}>
                    {u.username}{u.is_public_shared ? ' · Public' : ''}{u.is_active ? '' : ' (inactive)'}
                  </Tag>
                ))}</Space>
              : <Text type="secondary">none registered yet (register a user with role "Special")</Text>}
          </div>
        </div>,
      }]} />

      <Card>
        {loading ? <Spin /> : users.length === 0 ? <Empty description="No registered users" /> :
          <Table dataSource={users} columns={columns} rowKey="id" pagination={false} />}
      </Card>

      <Modal title="Register End-User" open={addOpen}
        onCancel={() => { setAddOpen(false); form.resetFields() }} footer={null}>
        <Form form={form} layout="vertical" onFinish={handleAdd}>
          <Form.Item name="username" label="Username" rules={[{ required: true }]}><Input placeholder="alice" /></Form.Item>
          <Form.Item name="password" label="Password" rules={[{ required: true, min: 4 }]}><Input.Password placeholder="****" /></Form.Item>
          <Form.Item name="role" label="Role" initialValue="viewer"
            extra="Viewer self-registration needs approval. Special is created approved/active by this admin route.">
            <Select options={[{ value: 'viewer', label: 'Viewer' }, { value: 'special', label: 'Special' }]} />
          </Form.Item>
          {roleValue === 'special' && (
            <Form.Item name="is_public_shared" label="Public functional user"
              valuePropName="checked" initialValue={false}
              extra="Only a special functional user can be public. This is chosen at registration and cannot be changed later.">
              <Switch checkedChildren="Public" unCheckedChildren="Private" />
            </Form.Item>
          )}
          <Button type="primary" htmlType="submit" loading={addLoading} block>Register</Button>
        </Form>
      </Modal>

      <Modal
        title={`Reset Password — ${pwdTarget?.username || ''}`}
        open={!!pwdTarget}
        onCancel={() => { setPwdTarget(null); pwdForm.resetFields() }}
        footer={null}
      >
        <Form form={pwdForm} layout="vertical" onFinish={resetPassword}>
          <Form.Item name="password" label="New Password" rules={[{ required: true, min: 4 }]}>
            <Input.Password placeholder="min 4 chars" />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={pwdLoading} block>Reset Password</Button>
        </Form>
      </Modal>

      <Modal
        title={`Allowed Shared Users for ${specialUsersTarget?.username || ''}`}
        open={specialUsersModalOpen}
        onCancel={() => setSpecialUsersModalOpen(false)}
        footer={<Button onClick={() => setSpecialUsersModalOpen(false)}>Close</Button>}
      >
        {modalTags.length === 0 ? (
          <Empty description="No eligible shared accounts. Register a special user first." />
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {modalTags.map((shared: any) => {
              const state = grantState(shared.id)
              const selected = state === 'active'
              const inactive = state === 'inactive'
              return (
                <Tag
                  key={shared.id}
                  color={selected
                    ? (shared.is_public_shared ? 'green' : 'purple') : 'default'}
                  style={{
                    cursor: grantLoading ? 'wait' : 'pointer',
                    padding: '4px 12px',
                    fontSize: 14,
                    opacity: selected ? 1 : 0.5,
                  }}
                  onClick={() => { if (!grantLoading) toggleGrant(shared, state) }}
                >
                  {selected ? '✓ ' : ''}{shared.username}
                  {shared.is_public_shared ? ' · Public' : ''}
                  {inactive ? ' (off)' : ''}
                  {shared.is_active === false ? ' (account inactive)' : ''}
                </Tag>
              )
            })}
          </div>
        )}
        <div style={{ marginTop: 16 }}>
          <Text type="secondary">
            Click a tag to toggle. A grey tag is an inactive row (or an unselected
            account); activation requires both accounts to be active. Deactivating
            an account is not undone by reactivating it — each tag is re-approved here.
          </Text>
        </div>
      </Modal>

      {/* Role change — typed confirmation (same pattern as Delete service). */}
      <Modal
        title={roleNext === 'admin'
          ? `Promote ${roleTarget?.username || ''} to admin — confirm`
          : `Demote ${roleTarget?.username || ''} to viewer — confirm`}
        open={!!roleTarget}
        onCancel={() => { setRoleTarget(null); setRoleInput('') }}
        okText={roleNext === 'admin' ? 'Promote' : 'Demote'}
        okButtonProps={{ danger: roleNext === 'viewer', disabled: roleInput !== roleToken }}
        confirmLoading={roleLoading}
        onOk={confirmRoleChange}
      >
        <p>
          {roleNext === 'admin'
            ? 'This grants the account every admin capability: service operations, user management and API-key administration.'
            : 'This removes every admin capability; the account keeps its own services and stream exposures.'}{' '}
          An admin cannot change its own role.
        </p>
        <p>Type <Text code>{roleToken}</Text> to confirm:</p>
        <Input value={roleInput} onChange={(e) => setRoleInput(e.target.value)}
          placeholder="confirm string" />
      </Modal>

      {/* Account delete — typed confirmation (same pattern as Delete service). */}
      <Modal
        title={`Delete ${delTarget?.username || ''} — confirm`}
        open={!!delTarget}
        onCancel={() => { setDelTarget(null); setDelInput('') }}
        okText="Delete"
        okButtonProps={{ danger: true, disabled: delInput !== delToken }}
        confirmLoading={delLoading}
        onOk={confirmDelete}
      >
        <p>
          Every service of this account is stopped first. If any stop fails,
          the account is NOT deleted and the failure is shown.
        </p>
        <p>
          On success the account is soft-deleted: the username stays reserved,
          grants/keys/sessions/exposures are revoked, and the stopped services
          remain as orphans for you to delete separately.
        </p>
        <p>Type <Text code>{delToken}</Text> to confirm:</p>
        <Input value={delInput} onChange={(e) => setDelInput(e.target.value)}
          placeholder="confirm string" />
      </Modal>
    </div>
  )
}
