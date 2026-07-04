import { Typography, Card, Row, Col, Statistic } from 'antd'
import { useAuth } from '../hooks/useAuth'

const { Title } = Typography

export default function DashboardPage() {
  const { admin } = useAuth()

  return (
    <div>
      <Title level={3}>Dashboard</Title>
      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic title="Services" value={0} />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic title="Users" value={0} />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic title="Running Tasks" value={0} />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic title="System Status" value="OK" />
          </Card>
        </Col>
      </Row>
      <Card style={{ marginTop: 16 }}>
        <Title level={5}>Welcome, {admin?.email || 'Admin'}!</Title>
        <p>Provision Gateway is running. Use the sidebar to manage services, users, and tasks.</p>
      </Card>
    </div>
  )
}
