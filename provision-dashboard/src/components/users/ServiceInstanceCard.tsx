import { Collapse, Tag, Space, Typography, Badge, Tooltip, Button } from 'antd'
import { RocketOutlined, PauseOutlined, CaretRightOutlined, DeleteOutlined, CopyOutlined, KeyOutlined, SwapOutlined, UnorderedListOutlined } from '@ant-design/icons'

const { Text } = Typography
const { Panel } = Collapse

interface ContainerInfo { [name: string]: string }

export interface ServiceInstance {
  user_name: string
  service_name: string
  label: string
  healthy_containers?: ContainerInfo
  unhealthy_containers?: ContainerInfo
  missing_containers?: ContainerInfo
  compose_template_path?: string
  nginx_conf_template_path?: string
  has_auth?: boolean
  url?: string
}

interface ServiceInstanceCardProps {
  svc: ServiceInstance
  key_: string
  isAdmin: boolean
  activeTasks: Record<string, string>
  needsRedeploy: Record<string, boolean>
  selectedKeys: Set<string>
  onToggleSelect: (key: string) => void
  onStartStop: (svc: ServiceInstance) => void
  onRebuild: (svc: ServiceInstance) => void
  onRedeploy: (svc: ServiceInstance) => void
  onChangePassword: (svc: ServiceInstance) => void
  onDuplicate: (svc: ServiceInstance) => void
  onDelete: (svc: ServiceInstance) => void
}

export default function ServiceInstanceCard({ svc, key_, isAdmin, activeTasks, needsRedeploy, onToggleSelect, onStartStop, onRebuild, onRedeploy, onChangePassword, onDuplicate, onDelete }: ServiceInstanceCardProps) {
  const containers = { ...svc.healthy_containers, ...svc.unhealthy_containers, ...svc.missing_containers }
  const hasHealthy = Object.keys(svc.healthy_containers || {}).length > 0

  const getBadge = () => {
    const hasMissing = Object.keys(svc.missing_containers || {}).length > 0
    const hasUnhealthy = Object.keys(svc.unhealthy_containers || {}).length > 0
    if (hasMissing && !hasHealthy) return <Badge status="error" />
    if (hasUnhealthy || hasMissing) return <Badge status="warning" />
    return <Badge status="success" />
  }

  return (
    <Panel
      key={key_}
      header={
        <Space>
          {getBadge()}
          <Text strong>{svc.service_name}</Text>
          <Tag>{svc.label}</Tag>
          {activeTasks[key_] && (
            <Button type="link" size="small" icon={<UnorderedListOutlined />} onClick={e => e.stopPropagation()}>Building...</Button>
          )}
        </Space>
      }
      extra={
        <Space onClick={e => e.stopPropagation()}>
          {isAdmin && <>
            <Tooltip title={hasHealthy ? 'Stop' : 'Start'}>
              <Button size="small" icon={hasHealthy ? <PauseOutlined /> : <CaretRightOutlined />}
                type={hasHealthy ? 'default' : 'primary'} onClick={() => onStartStop(svc)} />
            </Tooltip>
            <Tooltip title="Rebuild with cache"><Button size="small" onClick={() => onRebuild(svc)}>Rebuild</Button></Tooltip>
            <Tooltip title="Redeploy (no-cache)">
              <Button size="small" icon={<RocketOutlined />}
                className={needsRedeploy[key_] ? 'redeploy-blink' : ''}
                style={needsRedeploy[key_] ? { borderColor: '#faad14', color: '#faad14' } : {}}
                onClick={() => onRedeploy(svc)}>Redeploy</Button>
            </Tooltip>
            <Tooltip title="Change password"><Button size="small" icon={<KeyOutlined />} onClick={() => onChangePassword(svc)} /></Tooltip>
            <Tooltip title="Duplicate"><Button size="small" icon={<CopyOutlined />} onClick={() => onDuplicate(svc)}>Dup</Button></Tooltip>
            <Popconfirm title="Delete?" onConfirm={() => onDelete(svc)}>
              <Button size="small" danger icon={<DeleteOutlined />} />
            </Popconfirm>
          </>}
        </Space>
      }
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        {svc.url && (
          <div>
            <a href={svc.url} target="_blank" rel="noreferrer">{svc.url}</a>
            {svc.has_auth && <Tag style={{ marginLeft: 8 }}>🔒 auth</Tag>}
          </div>
        )}
        <div>
          <Space wrap>
            {Object.entries(containers).map(([name, status]) => (
              <Tag key={name} color={status === 'running' ? 'green' : status === 'exited' ? 'red' : 'orange'}>
                {name}: {status}
              </Tag>
            ))}
          </Space>
        </div>
      </Space>
    </Panel>
  )
}

// Need Popconfirm imported
import { Popconfirm } from 'antd'
