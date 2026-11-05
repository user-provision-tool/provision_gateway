import { ReactNode } from 'react'
import { Space, Tag, Typography } from 'antd'

const { Text } = Typography

/**
 * The service card's Endpoints + Stream endpoints body (R14/R6.2), shared by
 * the card itself and by the G86 `Can call` popup — the popup shows the same
 * existing card content, not a second simplified view.
 *
 * `streamActions` is the card's per-row action slot (Generate / Show / the
 * shown-once URL). The popup omits it: it is a view, never an editor.
 */
export interface ServiceEndpointsViewProps {
  svc: any
  goUrlFor: (svc: any, servedName: string) => string
  streamActions?: (canonical: string) => ReactNode
  showAuthTag?: boolean
}

export default function ServiceEndpointsView({
  svc, goUrlFor, streamActions, showAuthTag = true,
}: ServiceEndpointsViewProps) {
  const httpEndpoints: string[] = svc.http_endpoints || []
  const streamEndpoints: string[] = svc.stream_sub_services || []

  return (
    <>
      {httpEndpoints.length > 0 && (
        <div>
          <Text strong>Endpoints: </Text>
          {showAuthTag && svc.has_auth && <Tag color="blue" style={{marginLeft:4}}>Auth</Tag>}
          <Space direction="vertical" size={2}>
            {httpEndpoints.map((served: string) => {
              const sub = served.includes('--')
                ? served.split('--').slice(1).join('--').split('.')[0]
                : served
              return (
                <Space key={served} size={6} wrap>
                  <Text code>{sub}</Text>
                  <a href={goUrlFor(svc, served)} target="_blank" rel="noopener noreferrer">
                    <Text code style={{color:'#1677ff'}}>{served}</Text>
                  </a>
                </Space>
              )
            })}
          </Space>
        </div>
      )}
      {streamEndpoints.length > 0 && (
        <div>
          <Text strong>Stream endpoints: </Text>
          <Space direction="vertical" size={2}>
            {streamEndpoints.map((canonical: string) => {
              const sub = canonical.includes('--')
                ? canonical.split('--').slice(1).join('--').split('.')[0]
                : canonical
              return (
                <Space key={canonical} size={6} wrap>
                  <Text code>{sub}</Text>
                  {streamActions?.(canonical)}
                </Space>
              )
            })}
          </Space>
          <div>
            <Text type="secondary" style={{fontSize:12}}>
              The client must speak TLS — dial the name with the shared stream port
              (shown when you generate the URL). The application itself needs none.
            </Text>
          </div>
        </div>
      )}
    </>
  )
}
