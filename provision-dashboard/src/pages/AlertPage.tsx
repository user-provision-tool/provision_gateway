import { useSearchParams } from 'react-router-dom'
import { Typography, Card, Result, Button } from 'antd'
import { WarningOutlined, ClockCircleOutlined } from '@ant-design/icons'

const { Text } = Typography

export default function AlertPage() {
  const [searchParams] = useSearchParams()
  const reason = searchParams.get('reason') || ''

  if (reason === 'token_expired') {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '60vh' }}>
        <Card style={{ maxWidth: 500, textAlign: 'center' }}>
          <Result
            icon={<ClockCircleOutlined style={{ color: '#faad14', fontSize: 64 }} />}
            title="API Token Expired"
            subTitle="Your API token has expired. Please log in again to get a new token."
            extra={
              <Button type="primary" href="/login" size="large">
                Go to Login
              </Button>
            }
          />
        </Card>
      </div>
    )
  }

  if (reason === 'acl_denied') {
    const service = searchParams.get('service') || 'this service'
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '60vh' }}>
        <Card style={{ maxWidth: 500, textAlign: 'center' }}>
          <Result
            icon={<WarningOutlined style={{ color: '#ff4d4f', fontSize: 64 }} />}
            title="Access Denied"
            subTitle={`You do not have access to ${service}. Contact your administrator if you believe this is an error.`}
            extra={
              <Button type="primary" href="/dashboard" size="large">
                Back to Dashboard
              </Button>
            }
          />
        </Card>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '60vh' }}>
      <Card style={{ maxWidth: 500, textAlign: 'center' }}>
        <Result
          status="info"
          title="Alert"
          subTitle="An unknown alert was triggered."
          extra={
            <Button type="primary" href="/dashboard" size="large">
              Back to Dashboard
            </Button>
          }
        />
      </Card>
    </div>
  )
}
