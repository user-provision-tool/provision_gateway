import { useState, useEffect, useRef } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { Layout, Menu, Button, Typography, Dropdown, theme, Modal, Input, message, Space, Tag } from 'antd'
import {
  DashboardOutlined,
  AppstoreOutlined,
  UserOutlined,
  UnorderedListOutlined,
  SettingOutlined,
  AuditOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  KeyOutlined,
  QuestionCircleOutlined,
} from '@ant-design/icons'
import { useAuth } from '../../hooks/useAuth'
import client from '../../api/client'

const { Header, Sider, Content, Footer } = Layout
const { Text } = Typography

const menuItems = [
  { key: '/dashboard', icon: <DashboardOutlined />, label: 'Dashboard' },
  { key: '/services', icon: <AppstoreOutlined />, label: 'Source Projects' },
  { key: '/users', icon: <UserOutlined />, label: 'Services' },
  { key: '/tasks', icon: <UnorderedListOutlined />, label: 'Tasks' },
  { key: '/settings', icon: <SettingOutlined />, label: 'Settings' },
  { key: '/audit', icon: <AuditOutlined />, label: 'Audit' },
  { key: '/users/manage', icon: <UserOutlined />, label: 'Users' },
]

export default function AppLayout() {
  const [collapsed, setCollapsed] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()
  const { admin, logout } = useAuth()
  const { token: themeToken } = theme.useToken()
  const [pwdModalOpen, setPwdModalOpen] = useState(false)
  const [currentPwd, setCurrentPwd] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [pwdLoading, setPwdLoading] = useState(false)

  // Troubleshoot chat
  const [chatOpen, setChatOpen] = useState(false)
  const [chatInput, setChatInput] = useState('')
  const [chatHistory, setChatHistory] = useState<{role:string;content:string}[]>([])
  const [chatLoading, setChatLoading] = useState(false)

  const isAdmin = admin?.role === 'admin'

  const handleMenuClick = ({ key }: { key: string }) => {
    navigate(key)
  }

  const handleChangePassword = async () => {
    if (!currentPwd || !newPwd) { message.warning('Fill both fields'); return }
    setPwdLoading(true)
    try {
      await client.put('/auth/password', { current_password: currentPwd, new_password: newPwd })
      message.success('Password changed')
      setPwdModalOpen(false)
      setCurrentPwd('')
      setNewPwd('')
    } catch (err: any) { message.error(err.response?.data?.detail || 'Failed') }
    finally { setPwdLoading(false) }
  }

  const userMenuItems = [
    { key: 'password', icon: <KeyOutlined />, label: 'Change Password' },
    { key: 'logout', icon: <LogoutOutlined />, label: 'Logout', danger: true },
  ]

  const handleUserMenuClick = ({ key }: { key: string }) => {
    if (key === 'logout') logout()
    if (key === 'password') setPwdModalOpen(true)
  }

  const handleTroubleshoot = async () => {
    if (!chatInput.trim()) return
    const userMsg = chatInput.trim()
    setChatInput('')
    setChatHistory(h => [...h, {role:'user',content:userMsg}])
    setChatLoading(true)
    try {
      const { data } = await client.post('/llm/generate', { prompt: userMsg, generate_type: 'troubleshoot' })
      setChatHistory(h => [...h, {role:'assistant',content:data.response||data.answer||JSON.stringify(data)}])
    } catch (err: any) {
      setChatHistory(h => [...h, {role:'assistant',content:'Error: '+(err.response?.data?.detail||'Failed to get response')}])
    }
    finally { setChatLoading(false) }
  }

  // Request notification permission and poll for completed tasks
  const notifiedRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    if ('Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission()
    }
    const poll = async () => {
      try {
        const { data } = await client.get('/tasks')
        const tasks = data.tasks || data || []
        for (const t of tasks) {
          if ((t.status === 'completed' || t.status === 'succeeded' || t.status === 'failed') && !notifiedRef.current.has(t.id)) {
            notifiedRef.current.add(t.id)
            const emoji = t.status === 'failed' ? '❌' : '✅'
            if ('Notification' in window && Notification.permission === 'granted') {
              try { new Notification(`Task ${t.status}`, { body: `${t.type||'task'} ${t.target||''}`, icon: '/favicon.ico' }) } catch {}
            }
            message.info({ content: `${emoji} Task ${t.id?.substring(0,8)}... ${t.status}`, duration: 5 })
          }
        }
      } catch { /* silent */ }
    }
    const id = setInterval(poll, 15000)
    return () => clearInterval(id)
  }, [])

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        trigger={null}
        collapsible
        collapsed={collapsed}
        theme="dark"
        width={220}
        style={{
          overflow: 'auto',
          height: '100vh',
          position: 'fixed',
          left: 0,
          top: 0,
          bottom: 0,
          zIndex: 10,
        }}
      >
        <div style={{
          height: 64,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          borderBottom: '1px solid rgba(255,255,255,0.1)',
        }}>
          <Text strong style={{ color: '#fff', fontSize: collapsed ? 14 : 18 }}>
            {collapsed ? 'PG' : 'Provision Gateway'}
          </Text>
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[location.pathname.startsWith('/services') ? '/services' :
                         location.pathname.startsWith('/users/manage') ? '/users/manage' :
                         location.pathname.startsWith('/users') ? '/users' : location.pathname]}
          items={isAdmin ? menuItems : menuItems.filter(m => ['/dashboard','/users','/tasks','/audit'].includes(m.key))}
          onClick={handleMenuClick}
        />
      </Sider>
      <Layout style={{ marginLeft: collapsed ? 80 : 220, transition: 'all 0.2s' }}>
        <Header style={{
          padding: '0 24px',
          background: themeToken.colorBgContainer,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderBottom: '1px solid #f0f0f0',
          position: 'sticky',
          top: 0,
          zIndex: 9,
        }}>
          <Button
            type="text"
            icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            onClick={() => setCollapsed(!collapsed)}
          />
          <div style={{display:'flex',alignItems:'center',gap:8}}>
            {isAdmin && <Button type="text" icon={<QuestionCircleOutlined/>} onClick={()=>setChatOpen(true)} title="Troubleshoot"/>}
            <Dropdown
              menu={{ items: userMenuItems, onClick: handleUserMenuClick }}
              placement="bottomRight"
            >
              <Button type="text" icon={<UserOutlined />}>
                {admin?.email || 'Admin'}
              </Button>
            </Dropdown>
          </div>
        </Header>
        <Content style={{ margin: 24, minHeight: 280 }}>
          <Outlet />
        </Content>
        <Footer style={{ textAlign: 'center', padding: '12px 50px' }}>
          <Text type="secondary">Provision Gateway v1.0.0 © 2026</Text>
        </Footer>
      </Layout>

      {/* Password change modal */}
      <Modal
        title="Change Password"
        open={pwdModalOpen}
        onCancel={()=>{setPwdModalOpen(false);setCurrentPwd('');setNewPwd('')}}
        onOk={handleChangePassword}
        confirmLoading={pwdLoading}
        okText="Change"
      >
        <Space direction="vertical" style={{width:'100%'}} size="middle">
          <Input.Password prefix={<KeyOutlined/>} placeholder="Current password" value={currentPwd} onChange={e=>setCurrentPwd(e.target.value)}/>
          <Input.Password prefix={<KeyOutlined/>} placeholder="New password (min 6 chars)" value={newPwd} onChange={e=>setNewPwd(e.target.value)}/>
        </Space>
      </Modal>

      {/* Troubleshoot chat modal */}
      <Modal
        title={<span><QuestionCircleOutlined/> Troubleshoot Assistant</span>}
        open={chatOpen}
        onCancel={()=>setChatOpen(false)}
        footer={null}
        width={600}
      >
        <div style={{minHeight:300,maxHeight:'60vh',overflow:'auto',marginBottom:12,border:'1px solid #f0f0f0',borderRadius:8,padding:12,background:'#fafafa'}}>
          {chatHistory.length===0 ? <Text type="secondary">Ask me anything about your services, deployment issues, or configuration problems.</Text> :
            chatHistory.map((msg,i)=><div key={i} style={{marginBottom:8,textAlign:msg.role==='user'?'right':'left'}}>
              <Tag color={msg.role==='user'?'blue':'green'}>{msg.role}</Tag>
              <div style={{whiteSpace:'pre-wrap',wordBreak:'break-word',marginTop:4,fontSize:13}}>{msg.content}</div>
            </div>)}
        </div>
        <Space.Compact style={{width:'100%'}}>
          <Input placeholder="e.g. My service for alice is down, what should I check?" value={chatInput} onChange={e=>setChatInput(e.target.value)} onPressEnter={handleTroubleshoot}/>
          <Button type="primary" onClick={handleTroubleshoot} loading={chatLoading}>Ask</Button>
        </Space.Compact>
      </Modal>
    </Layout>
  )
}
