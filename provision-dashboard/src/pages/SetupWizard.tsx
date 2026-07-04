import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Form, Input, Button, Typography, message, Card, Steps } from 'antd'
import { UserOutlined, LockOutlined, CheckCircleOutlined } from '@ant-design/icons'
import { useAuth } from '../hooks/useAuth'

const { Title, Text } = Typography

export default function SetupWizard() {
  const [loading, setLoading] = useState(false)
  const [step, setStep] = useState(0)
  const { setup } = useAuth()
  const navigate = useNavigate()

  const handleSetup = async (values: { email: string; password: string }) => {
    setLoading(true)
    try {
      await setup(values.email, values.password)
      setStep(1)
      message.success('Admin account created!')
      setTimeout(() => navigate('/dashboard'), 1500)
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Setup failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="setup-container">
      <Card style={{ width: 500, padding: 24 }}>
        <Title level={2} style={{ textAlign: 'center' }}>Welcome to Provision Gateway</Title>
        <Text type="secondary" style={{ display: 'block', textAlign: 'center', marginBottom: 24 }}>
          Create your admin account to get started.
        </Text>
        <Steps
          current={step}
          items={[
            { title: 'Create Admin', icon: <UserOutlined /> },
            { title: 'Done', icon: <CheckCircleOutlined /> },
          ]}
          style={{ marginBottom: 32 }}
        />
        {step === 0 && (
          <Form onFinish={handleSetup} layout="vertical" size="large">
            <Form.Item
              name="email"
              rules={[{ required: true, message: 'Please enter your email' }]}
            >
              <Input prefix={<UserOutlined />} placeholder="Admin Email" />
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
              name="confirm"
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
              <Button type="primary" htmlType="submit" loading={loading} block>
                Create Admin Account
              </Button>
            </Form.Item>
          </Form>
        )}
        {step === 1 && (
          <div style={{ textAlign: 'center', padding: 24 }}>
            <CheckCircleOutlined style={{ fontSize: 48, color: '#52c41a' }} />
            <Title level={4}>Setup Complete!</Title>
            <Text>Redirecting to dashboard...</Text>
          </div>
        )}
      </Card>
    </div>
  )
}
