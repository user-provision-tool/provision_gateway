import { Typography, Card, Empty } from 'antd'

const { Title } = Typography

export default function AuditPage() {
  return (
    <div>
      <Title level={3}>Audit Log</Title>
      <Card>
        <Empty description="No audit entries yet." />
      </Card>
    </div>
  )
}
