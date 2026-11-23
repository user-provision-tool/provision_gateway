import { Button, Modal, Select, Space, Tag, Typography } from 'antd'
import {
  FileSetCandidates,
  FileSetSelectionData,
  SelectedFileRow,
} from './FileSetSelection'

const { Text } = Typography

/** The per-slot base refs the generation request carries. `null` is the
 *  `None` choice; a filled slot contributes no ref (R1/R5). */
export interface BaseRefs {
  compose: string[]
  serving_ports: string | null
  nginx: string | null
  env: string[]
}

export const EMPTY_BASE_REFS: BaseRefs = {
  compose: [],
  serving_ports: null,
  nginx: null,
  env: [],
}

export type BaseRefSlot = 'compose' | 'serving_ports' | 'nginx' | 'env'

export interface BaseRefSlotSpec {
  key: BaseRefSlot
  label: string
  multi: boolean
}

/** The deploy panel mirrors its FOUR sent-file pickers. */
export const DEPLOY_REF_SLOTS: BaseRefSlotSpec[] = [
  { key: 'compose', label: 'Compose files', multi: true },
  { key: 'serving_ports', label: 'Serving ports', multi: false },
  { key: 'nginx', label: 'Nginx config', multi: false },
  { key: 'env', label: 'Env files', multi: true },
]

/** The Source Projects modal has THREE sent-file pickers and no env phase. */
export const SOURCE_PROJECT_REF_SLOTS: BaseRefSlotSpec[] = [
  { key: 'compose', label: 'Compose files', multi: true },
  { key: 'serving_ports', label: 'Serving ports', multi: false },
  { key: 'nginx', label: 'Nginx config', multi: false },
]

/** R1/AC2 — a ref picker is enabled only while its sent picker is on
 *  `generate new` (no selected file and not the deliberate None). */
export function refSlotEnabled(slot: BaseRefSlot, selection: FileSetSelectionData): boolean {
  if (slot === 'compose') return selection.compose.length === 0
  if (slot === 'serving_ports') {
    return !selection.serving_ports && !selection.serving_ports_none
  }
  if (slot === 'nginx') {
    return !selection.nginx && !selection.stream_conf && !selection.mail_conf
  }
  return selection.env.length === 0 && !selection.env_none
}

/** Drop refs whose sent slot is no longer on `generate new`; the server
 *  refuses a ref on a filled slot, so the panel must never send one. */
export function activeBaseRefs(refs: BaseRefs, selection: FileSetSelectionData): BaseRefs {
  return {
    compose: refSlotEnabled('compose', selection) ? refs.compose : [],
    serving_ports: refSlotEnabled('serving_ports', selection) ? refs.serving_ports : null,
    nginx: refSlotEnabled('nginx', selection) ? refs.nginx : null,
    env: refSlotEnabled('env', selection) ? refs.env : [],
  }
}

function optionsFor(slot: BaseRefSlot, candidates: FileSetCandidates): { value: string; label: string }[] {
  const list =
    slot === 'compose' ? candidates.compose
      : slot === 'serving_ports' ? (candidates.serving_ports || [])
        : slot === 'nginx'
          ? [...(candidates.nginx || []), ...(candidates.stream_conf || []), ...(candidates.mail_conf || [])]
          : (candidates.env || [])
  return list.map(c => ({ value: c, label: c }))
}

interface Props {
  open: boolean
  onClose: () => void
  slots: BaseRefSlotSpec[]
  candidates: FileSetCandidates
  selection: FileSetSelectionData
  refs: BaseRefs
  onChange: (refs: BaseRefs) => void
  onOpenFile: (name: string) => void
}

export default function BaseFilesPopup({
  open, onClose, slots, candidates, selection, refs, onChange, onOpenFile,
}: Props) {
  const setSlot = (key: BaseRefSlot, value: string[] | string | null) => {
    onChange({ ...refs, [key]: value } as BaseRefs)
  }

  return (
    <Modal
      title="Base files"
      open={open}
      onCancel={onClose}
      onOk={onClose}
      okText="Done"
      width={680}
      destroyOnClose
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {slots.map(slot => {
          const enabled = refSlotEnabled(slot.key, selection)
          const options = optionsFor(slot.key, candidates)
          const value = refs[slot.key]
          const rows = slot.multi
            ? (value as string[])
            : (value ? [value as string] : [])
          return (
            <div key={slot.key}>
              <Text strong>{slot.label}</Text>
              {!enabled && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {' '}— disabled: this slot sends a selected file, so it is not generated
                </Text>
              )}
              {rows.map(name => (
                <SelectedFileRow
                  key={name}
                  name={name}
                  disabled={!enabled}
                  onOpen={onOpenFile}
                  onRemove={() => {
                    if (!enabled) return
                    if (slot.multi) {
                      setSlot(slot.key, (value as string[]).filter(x => x !== name))
                    } else {
                      setSlot(slot.key, null)
                    }
                  }}
                />
              ))}
              {slot.multi ? (
                <>
                  <Select
                    mode="multiple"
                    allowClear
                    style={{ width: '100%', marginTop: 4 }}
                    disabled={!enabled}
                    value={[]}
                    placeholder={enabled
                      ? ((value as string[]).length ? 'Add another base file…' : 'None — no base constraint')
                      : 'Disabled while this slot sends a selected file'}
                    onChange={(picked) => {
                      const next = (picked as string[]).filter(Boolean)
                      if (next.length === 0) return
                      setSlot(slot.key, [...new Set([...(value as string[]), ...next])])
                    }}
                    options={options.filter(o => !(value as string[]).includes(o.value))}
                  />
                  {(value as string[]).length === 0 ? (
                    <Tag color="default" style={{ marginTop: 6 }}>None — no base constraint</Tag>
                  ) : (
                    <Button size="small" type="link" style={{ paddingLeft: 0 }}
                      onClick={() => setSlot(slot.key, [])}>Clear</Button>
                  )}
                </>
              ) : (
                <Select
                  style={{ width: '100%', marginTop: 4 }}
                  disabled={!enabled}
                  value={(value as string | null) || '__none__'}
                  onChange={(v) => setSlot(slot.key, v === '__none__' ? null : String(v))}
                  options={[{ value: '__none__', label: 'None' }, ...options]}
                />
              )}
            </div>
          )
        })}
      </Space>
    </Modal>
  )
}
