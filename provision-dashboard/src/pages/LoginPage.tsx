import { useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { Form, Input, Button, Typography, message, Card, Divider, Modal } from 'antd'
import { UserOutlined, LockOutlined, MailOutlined } from '@ant-design/icons'
import { useAuth } from '../hooks/useAuth'
import client from '../api/client'

const { Title, Text } = Typography

export default function LoginPage() {
  const [loading, setLoading] = useState(false)
  const [registerModalOpen, setRegisterModalOpen] = useState(false)
  const [registerLoading, setRegisterLoading] = useState(false)
  const { login } = useAuth()
  const navigate = useNavigate()

  const handleSubmit = async (values: { email: string; password: string }) => {
    setLoading(true)
    try {
      await login(values.email, values.password)
      message.success('Login successful')
      navigate('/dashboard')
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  const handleRegister = async (values: { username: string; email: string; password: string }) => {
    setRegisterLoading(true)
    try {
      await client.post('/auth/users/register', {
        username: values.username,
        email: values.email,
        password: values.password,
      })
      message.success('Registration submitted! Please wait for admin approval.')
      setRegisterModalOpen(false)
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Registration failed')
    } finally {
      setRegisterLoading(false)
    }
  }

  return (
    <div className="login-container">
      <Card className="login-card">
        <Title level={2}>Provision Gateway</Title>
        <Form onFinish={handleSubmit} layout="vertical" size="large">
          <Form.Item
            name="email"
            rules={[{ required: true, message: 'Please enter your email' }]}
          >
            <Input prefix={<UserOutlined />} placeholder="Email" />
          </Form.Item>
          <Form.Item
            name="password"
            rules={[{ required: true, message: 'Please enter your password' }]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="Password" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={loading} block>
              Log In
            </Button>
          </Form.Item>
          <Divider plain><Text type="secondary">or</Text></Divider>
          <div style={{ textAlign: 'center' }}>
            <Button type="link" onClick={() => setRegisterModalOpen(true)}>
              Register new account
            </Button>
          </div>
        </Form>
      </Card>

      {/* Register Modal */}
      <Modal
        title="Register New Account"
        open={registerModalOpen}
        onCancel={() => setRegisterModalOpen(false)}
        footer={null}
        destroyOnClose
      >
        <Form onFinish={handleRegister} layout="vertical" size="large">
          <Form.Item
            name="username"
            rules={[{ required: true, message: 'Please enter a username' }]}
          >
            <Input prefix={<UserOutlined />} placeholder="Username" />
          </Form.Item>
          <Form.Item
            name="email"
            rules={[
              { required: true, message: 'Please enter your email' },
              { type: 'email', message: 'Please enter a valid email' },
            ]}
          >
            <Input prefix={<MailOutlined />} placeholder="Email" />
          </Form.Item>
          <Form.Item
            name="password"
            rules={[
              { required: true, message: 'Please enter a password' },
              { min: 6, message: 'Password must be at least 6 characters' },
            ]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="Password" />
          </Form.Item>
          <Form.Item
            name="confirmPassword"
            dependencies={['password']}
            rules={[
              { required: true, message: 'Please confirm your password' },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('password') === value) {
                    return Promise.resolve()
                  }
                  return Promise.reject(new Error('Passwords do not match'))
                },
              }),
            ]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="Confirm Password" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={registerLoading} block>
              Register
            </Button>
          </Form.Item>
          <Text type="secondary">
            Registration requires admin approval before you can log in.
          </Text>
        </Form>
      </Modal>
    </div>
  )
}
