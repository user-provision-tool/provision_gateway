import { Typography, Card, Empty } from 'antd'

const { Title } = Typography

export default function TasksPage() {
  return (
    <div>
      <Title level={3}>Tasks</Title>
      <Card>
        <Empty description="No tasks in queue." />
      </Card>
    </div>
  )
}
