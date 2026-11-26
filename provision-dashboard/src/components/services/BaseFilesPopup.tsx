import { Button, Modal, Select, Space, Tag, Typography } from 'antd'
import { ArrowDownOutlined, ArrowUpOutlined } from '@ant-design/icons'
import {
  FileSetCandidates,
  FileSetSelectionData,
  SelectedFileRow,
  confCategory,
} from './FileSetSelection'

const { Text } = Typography

/** The Nginx base ref is a per-category set (C15), at most one file per
 *  category; the picker is still the one Nginx picker (C14). */
export interface NginxBaseRefs {
  http: string | null
  stream: string | null
  mail: string | null
}

/** The per-slot base refs the generation request carries. `null`/all-null is
 *  the `None` choice; a filled slot contributes no ref (R1/R5). */
export interface BaseRefs {
  compose: string[]
  serving_ports: string | null
  nginx: NginxBaseRefs
  env: string[]
}

export const EMPTY_NGINX_BASE_REFS: NginxBaseRefs = {
  http: null, stream: null, mail: null,
}

export const EMPTY_BASE_REFS: BaseRefs = {
  compose: [],
  serving_ports: null,
  nginx: { ...EMPTY_NGINX_BASE_REFS },
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
    nginx: refSlotEnabled('nginx', selection)
      ? { ...refs.nginx }
      : { ...EMPTY_NGINX_BASE_REFS },
    env: refSlotEnabled('env', selection) ? refs.env : [],
  }
}

/** C14 — the black in-field empty value every base picker shows. */
const NONE_LABEL = 'None - no base constraint'
const NONE = '__base_none__'

function moveItem(list: string[], idx: number, dir: -1 | 1): string[] {
  const next = [...list]
  const j = idx + dir
  if (j < 0 || j >= next.length) return next
  ;[next[idx], next[j]] = [next[j], next[idx]]
  return next
}

/** C14 — the sent ordered-multi picker, reused exactly: rows with reorder,
 *  then ONE add select whose empty value is the black in-field None. No tag
 *  beside it, no Clear. */
function OrderedBasePicker({ values, candidates, disabled, onOpenFile, onChange, addPlaceholder }: {
  values: string[]
  candidates: string[]
  disabled: boolean
  onOpenFile: (name: string) => void
  onChange: (next: string[]) => void
  addPlaceholder: string
}) {
  const remaining = candidates.filter(c => !values.includes(c))
  return (
    <Space direction="vertical" style={{ width: '100%' }} size={4}>
      {values.map((v, i) => (
        <Space key={v} style={{ display: 'flex' }} size={4}>
          <Button size="small" icon={<ArrowUpOutlined />} disabled={disabled || i === 0}
            onClick={() => onChange(moveItem(values, i, -1))} />
          <Button size="small" icon={<ArrowDownOutlined />} disabled={disabled || i === values.length - 1}
            onClick={() => onChange(moveItem(values, i, 1))} />
          <SelectedFileRow
            name={v}
            onOpen={onOpenFile}
            disabled={disabled}
            onRemove={() => onChange(values.filter(x => x !== v))}
          />
        </Space>
      ))}
      <Select
        style={{ width: '100%' }}
        disabled={disabled}
        value={values.length === 0 ? NONE : undefined}
        placeholder={values.length === 0 ? undefined : addPlaceholder}
        onChange={(v) => {
          if (v === NONE || v === undefined) { onChange([]); return }
          onChange([...values, String(v)])
        }}
        options={[
          { value: NONE, label: NONE_LABEL },
          ...remaining.map(c => ({ value: c, label: c })),
        ]}
      />
    </Space>
  )
}

/** C14 — the sent single picker, reused exactly: a selected file is a row
 *  above; the control is the chooser whose empty value is the black None. */
function SingleBasePicker({ value, candidates, disabled, onOpenFile, onChange }: {
  value: string | null
  candidates: string[]
  disabled: boolean
  onOpenFile: (name: string) => void
  onChange: (next: string | null) => void
}) {
  return (
    <Space direction="vertical" style={{ width: '100%' }} size={4}>
      {value && (
        <SelectedFileRow
          name={value}
          onOpen={onOpenFile}
          disabled={disabled}
          onRemove={() => onChange(null)}
        />
      )}
      <Select
        style={{ width: '100%' }}
        disabled={disabled}
        value={value ? undefined : NONE}
        placeholder={value ? 'Select another base file…' : undefined}
        onChange={(v) => onChange(v === NONE ? null : String(v))}
        options={[
          { value: NONE, label: NONE_LABEL },
          ...candidates.filter(c => c !== value).map(c => ({ value: c, label: c })),
        ]}
      />
    </Space>
  )
}

/** C14 — the sent Nginx picker, reused exactly: selected confs are rows above;
 *  while none is selected the control is a SINGLE select showing the black
 *  None; once one is selected it is the ADD-MODE multi-select whose picks
 *  merge per category, each option carrying its blue type tag. C15 keeps one
 *  file per category in the ref set. */
function NginxBasePicker({ value, candidates, disabled, onOpenFile, onChange }: {
  value: NginxBaseRefs
  candidates: FileSetCandidates
  disabled: boolean
  onOpenFile: (name: string) => void
  onChange: (next: NginxBaseRefs) => void
}) {
  const selected = [value.http, value.stream, value.mail].filter(Boolean) as string[]
  const selectedCats = new Set(selected.map(confCategory))
  const options = [...new Set([
    ...(candidates.nginx || []),
    ...(candidates.stream_conf || []),
    ...(candidates.mail_conf || []),
  ])].filter(c => !selected.includes(c)).map(c => ({
    value: c,
    label: c,
    disabled: selectedCats.has(confCategory(c)),
  }))
  const clearCat = (name: string) => {
    const cat = confCategory(name)
    onChange(cat === 'stream'
      ? { ...value, stream: null }
      : cat === 'mail'
      ? { ...value, mail: null }
      : { ...value, http: null })
  }
  return (
    <Space direction="vertical" style={{ width: '100%' }} size={4}>
      {selected.map(name => (
        <SelectedFileRow
          key={name}
          name={name}
          onOpen={onOpenFile}
          disabled={disabled}
          onRemove={() => clearCat(name)}
        />
      ))}
      <Select
        mode={selected.length === 0 ? undefined : 'multiple'}
        style={{ width: '100%' }}
        disabled={disabled}
        value={selected.length === 0 ? NONE : []}
        placeholder={selected.length > 0 ? 'Add a config of another category…' : undefined}
        onChange={(vals) => {
          const arr: string[] = Array.isArray(vals) ? vals : (vals ? [String(vals)] : [])
          const picked = arr.filter(v => v !== NONE)
          if (picked.length === 0) {
            if (arr.includes(NONE)) onChange({ ...EMPTY_NGINX_BASE_REFS })
            return
          }
          const next: NginxBaseRefs = { ...value }
          for (const v of picked) {
            if (selected.includes(v)) continue
            const cat = confCategory(v)
            if (cat === 'stream') next.stream = v
            else if (cat === 'mail') next.mail = v
            else next.http = v
          }
          onChange(next)
        }}
        options={[
          { value: NONE, label: NONE_LABEL },
          ...options,
        ]}
        optionRender={(o) => (
          <span>
            {o.label}
            {o.value !== NONE && (
              <Tag color="blue" style={{ marginLeft: 8 }}>{confCategory(o.value as string)}</Tag>
            )}
          </span>
        )}
      />
    </Space>
  )
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
          return (
            <div key={slot.key}>
              <Text strong>{slot.label}</Text>
              {!enabled && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {' '}— disabled: this slot sends a selected file, so it is not generated
                </Text>
              )}
              {slot.key === 'compose' && (
                <OrderedBasePicker
                  values={refs.compose}
                  candidates={candidates.compose}
                  disabled={!enabled}
                  onOpenFile={onOpenFile}
                  addPlaceholder="Add another base file…"
                  onChange={(next) => onChange({ ...refs, compose: next })}
                />
              )}
              {slot.key === 'env' && (
                <OrderedBasePicker
                  values={refs.env}
                  candidates={candidates.env}
                  disabled={!enabled}
                  onOpenFile={onOpenFile}
                  addPlaceholder="Add another base file…"
                  onChange={(next) => onChange({ ...refs, env: next })}
                />
              )}
              {slot.key === 'serving_ports' && (
                <SingleBasePicker
                  value={refs.serving_ports}
                  candidates={candidates.serving_ports || []}
                  disabled={!enabled}
                  onOpenFile={onOpenFile}
                  onChange={(next) => onChange({ ...refs, serving_ports: next })}
                />
              )}
              {slot.key === 'nginx' && (
                <NginxBasePicker
                  value={refs.nginx}
                  candidates={candidates}
                  disabled={!enabled}
                  onOpenFile={onOpenFile}
                  onChange={(next) => onChange({ ...refs, nginx: next })}
                />
              )}
            </div>
          )
        })}
      </Space>
    </Modal>
  )
}
