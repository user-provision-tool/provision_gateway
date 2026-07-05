import { useState, useEffect } from 'react'
import { Typography, Card, Table, Tag, Space, Button, message, Spin, Empty, Modal, Form, Input, Select, Collapse } from 'antd'
import { UserAddOutlined, CheckOutlined, CloseOutlined, ReloadOutlined, SettingOutlined } from '@ant-design/icons'
import client from '../api/client'

const { Title, Text } = Typography

export default function UserManagementPage() {
  const [users, setUsers] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [addOpen, setAddOpen] = useState(false)
  const [addLoading, setAddLoading] = useState(false)
  const [form] = Form.useForm()
  
  // Per-user special users assignment
  const [specialUsersModalOpen, setSpecialUsersModalOpen] = useState(false)
  const [specialUsersTarget, setSpecialUsersTarget] = useState<any>(null)
  const [specialUsersGlobal, setSpecialUsersGlobal] = useState<string[]>([])
  const [specialUsersSelected, setSpecialUsersSelected] = useState<string[]>([])
  const [specialUsersLoading, setSpecialUsersLoading] = useState(false)

  useEffect(() => { loadUsers(); loadGlobalSpecialUsers() }, [])

  const loadUsers = async () => {
    setLoading(true)
    try { const { data } = await client.get('/auth/users'); setUsers(data.users || []) }
    catch (err: any) { message.error('Failed to load users') }
    finally { setLoading(false) }
  }

  const loadGlobalSpecialUsers = async () => {
    try {
      const { data } = await client.get('/system/config')
      const specialUsers = data.special_users || data.config?.special_users || []
      setSpecialUsersGlobal(Array.isArray(specialUsers) ? specialUsers : (typeof specialUsers==='string' ? specialUsers.split(',').map((s:string)=>s.trim()).filter(Boolean) : []))
    } catch { /* use defaults */ }
  }

  const handleAdd = async (values: any) => {
    setAddLoading(true)
    try { await client.post('/auth/users/register', values); message.success('User registered'); setAddOpen(false); form.resetFields(); loadUsers() }
    catch (err: any) { message.error(err.response?.data?.detail || 'Failed') }
    finally { setAddLoading(false) }
  }

  const handleApprove = async (id: number) => {
    try { await client.put(`/auth/users/${id}/approve`); message.success('Approved'); loadUsers() }
    catch (err: any) { message.error('Failed') }
  }

  const handleDelete = async (id: number) => {
    try { await client.delete(`/auth/users/${id}`); message.success('Deleted'); loadUsers() }
    catch (err: any) { message.error('Failed') }
  }

  const openSpecialUsers = (user: any) => {
    setSpecialUsersTarget(user)
    setSpecialUsersSelected(user.allowed_special_users || [])
    setSpecialUsersModalOpen(true)
  }

  const saveSpecialUsers = async () => {
    if (!specialUsersTarget) return
    setSpecialUsersLoading(true)
    try {
      await client.put(`/auth/users/${specialUsersTarget.id}`, {
        allowed_special_users: specialUsersSelected
      })
      message.success('Special users updated')
      setSpecialUsersModalOpen(false)
      loadUsers()
    } catch (err: any) { message.error(err.response?.data?.detail || 'Failed') }
    finally { setSpecialUsersLoading(false) }
  }

  const toggleSpecialUser = (name: string) => {
    setSpecialUsersSelected(prev =>
      prev.includes(name) ? prev.filter(s => s !== name) : [...prev, name]
    )
  }

  const columns = [
    { title: 'Username', dataIndex: 'username', key: 'username', render: (t:string) => <strong>{t}</strong> },
    { title: 'Role', dataIndex: 'role', key: 'role', render: (r:string) => <Tag color={r==='viewer'?'blue':'orange'}>{r}</Tag> },
    { title: 'Status', key: 'status', render: (_:any, r:any) => r.is_approved ? <Tag color="green">Approved</Tag> : <Tag color="gold">Pending</Tag> },
    { title: 'Allowed Special Users', key: 'special_users', render: (_:any, r:any) => {
      const allowed = r.allowed_special_users || []
      if (allowed.length === 0) return <Text type="secondary">none</Text>
      return <Space size={2} wrap>{allowed.map((s:string) => <Tag key={s} color="purple">{s}</Tag>)}</Space>
    }},
    { title: 'Actions', key: 'actions', render: (_:any, r:any) => <Space>
      <Button size="small" icon={<SettingOutlined/>} onClick={()=>openSpecialUsers(r)} title="Set allowed special functional users">Special</Button>
      {!r.is_approved && <Button size="small" type="primary" icon={<CheckOutlined/>} onClick={()=>handleApprove(r.id)}>Approve</Button>}
      <Button size="small" danger icon={<CloseOutlined/>} onClick={()=>handleDelete(r.id)}/>
    </Space> },
  ]

  return (
    <div>
      <div style={{display:'flex',justifyContent:'space-between',marginBottom:16}}>
        <Title level={3} style={{margin:0}}>User Management</Title>
        <Space>
          <Button type="primary" icon={<UserAddOutlined/>} onClick={()=>setAddOpen(true)}>Register User</Button>
          <Button icon={<ReloadOutlined/>} onClick={loadUsers}>Refresh</Button>
        </Space>
      </div>
      
      {/* Special Functional Users Configuration Card */}
      <Collapse style={{marginBottom:16}} items={[{
        key: 'special-users-config',
        label: <Space><SettingOutlined/><Text strong>Special Functional Users Configuration</Text></Space>,
        children: <div>
          <Text type="secondary">
            Special functional users (e.g. shared, public, internal) are service groups shared by multiple regular users.
            Below you can configure who can access which special user groups.
          </Text>
          <div style={{marginTop:12}}>
            <Text strong>Global Special Users: </Text>
            {specialUsersGlobal.length > 0
              ? <Space size={4} wrap>{specialUsersGlobal.map(s => <Tag key={s} color="purple">{s}</Tag>)}</Space>
              : <Text type="secondary">none configured (set in Settings → Special Users)</Text>
            }
          </div>
        </div>
      }]}/>
      
      <Card>
        {loading ? <Spin/> : users.length===0 ? <Empty description="No registered users"/> :
        <Table dataSource={users} columns={columns} rowKey="id" pagination={false}/>}
      </Card>
      
      <Modal title="Register End-User" open={addOpen} onCancel={()=>{setAddOpen(false);form.resetFields()}} footer={null}>
        <Form form={form} layout="vertical" onFinish={handleAdd}>
          <Form.Item name="username" label="Username" rules={[{required:true}]}><Input placeholder="alice"/></Form.Item>
          <Form.Item name="password" label="Password" rules={[{required:true,min:4}]}><Input.Password placeholder="****"/></Form.Item>
          <Form.Item name="role" label="Role" initialValue="viewer"><Select options={[{value:'viewer',label:'Viewer'},{value:'special',label:'Special'}]}/></Form.Item>
          <Button type="primary" htmlType="submit" loading={addLoading} block>Register</Button>
        </Form>
      </Modal>

      {/* Per-User Special Users Assignment Modal */}
      <Modal
        title={`Allowed Special Users for ${specialUsersTarget?.username || ''}`}
        open={specialUsersModalOpen}
        onCancel={() => setSpecialUsersModalOpen(false)}
        onOk={saveSpecialUsers}
        confirmLoading={specialUsersLoading}
        okText="Save"
      >
        {specialUsersGlobal.length === 0 ? (
          <Empty description="No global special users configured. Go to Settings → Special Users to add some." />
        ) : (
          <div style={{display:'flex',flexWrap:'wrap',gap:8}}>
            {specialUsersGlobal.map(name => {
              const isSelected = specialUsersSelected.includes(name)
              return (
                <Tag
                  key={name}
                  color={isSelected ? 'purple' : 'default'}
                  style={{cursor:'pointer',padding:'4px 12px',fontSize:14,opacity:isSelected?1:0.5}}
                  onClick={() => toggleSpecialUser(name)}
                >
                  {isSelected ? '✓ ' : ''}{name}
                </Tag>
              )
            })}
          </div>
        )}
        <div style={{marginTop:16}}>
          <Text type="secondary">Click tags to toggle. Selected users will be accessible to this viewer.</Text>
        </div>
      </Modal>
    </div>
  )
}
