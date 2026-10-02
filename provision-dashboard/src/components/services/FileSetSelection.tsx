import { Alert, Button, Select, Space, Tag, Typography } from 'antd'
import { ArrowDownOutlined, ArrowUpOutlined, CloseOutlined } from '@ant-design/icons'

/**
 * Per-recipe base-file selection UI — shared by the LLM generate-missing
 * panel and the deploy panel (design §Selection & UI).
 *
 * - compose: ordered multi (merge order is user-visible)
 * - nginx: single
 * - env: ordered multi (later wins), disabled/collapsed when needs_env=false
 * - profiles: multi-select dropdown (candidates derive from the SELECTED
 *   compose, so the section is disabled until the compose selection resolves)
 * - stale stored entries are kept + flagged (never dropped here); callers
 *   exclude them from generation/deploy requests.
 */

export interface FileSetSelectionData {
  compose: string[]
  nginx: string | null
  env: string[]
  profiles: string[]
}

export interface FileSetCandidates {
  compose: string[]
  nginx: string[]
  env: string[]
  profiles: string[]
}

export interface FileSetStale {
  compose?: string[]
  nginx?: string[]
  env?: string[]
  profiles?: string[]
}

export const EMPTY_SELECTION: FileSetSelectionData = { compose: [], nginx: null, env: [], profiles: [] }
export const EMPTY_CANDIDATES: FileSetCandidates = { compose: [], nginx: [], env: [], profiles: [] }

interface Props {
  selection: FileSetSelectionData
  candidates: FileSetCandidates
  stale?: FileSetStale
  needsEnv: boolean
  onChange: (next: FileSetSelectionData) => void
  // §5b: clicking a SELECTED file's name opens it for view/edit. Saving forks
  // into the `.fix` variant rather than writing back over the selection, so a
  // repo-original is never rewritten by an edit made here.
  onOpenFile?: (name: string) => void
}

export function fixVariantFor(name: string): string {
  // Mirrors the gateway's FIX_FILENAME: the tag goes before the extension and
  // is single-slotted (overwritten each round, never accumulated).
  if (/\.env/.test(name) && !name.includes('.raw.')) {
    const stem = name.split('.').slice(0, -1).join('.') || '.env'
    return `${stem}.fix`
  }
  const m = name.match(/^(.*)\.([^.]+)$/)
  return m ? `${m[1]}.fix.${m[2]}` : `${name}.fix`
}

/**
 * Which generation phase a file belongs to, read off the NAME'S SHAPE.
 *
 * Never a table of exact names: a generated compose/nginx name carries the
 * generation job's collision token — `docker-compose.base.337.yml`,
 * `nginx.fix.<job>.conf` (GRILLING G33) — so the exact-name checks this replaces
 * stopped matching the moment a token appeared. The two callers both treat a
 * MISSING validation as "valid" (`ok = !v || v.valid`), so the failure mode was
 * silent: every file rendered a green ✓ whatever its content.
 *
 * These are the platform's own candidate predicates (`_is_compose_candidate`,
 * the nginx `*.conf` rule), which the gateway and the -api both use — the three
 * MUST agree.
 */
export function phaseOfFile(filename: string): string {
  const base = filename.split('/').pop() || filename
  if (base.endsWith('.j2')) return phaseOfFile(base.slice(0, -3))
  if (base.startsWith('docker-compose') || base.startsWith('compose')) return 'compose'
  if (base.endsWith('.conf')) return 'nginx'
  if (base.startsWith('.env')) return 'env'
  return ''
}

function moveItem(list: string[], idx: number, dir: -1 | 1): string[] {
  const next = [...list]
  const j = idx + dir
  if (j < 0 || j >= next.length) return next
  ;[next[idx], next[j]] = [next[j], next[idx]]
  return next
}

function OrderedMulti({ values, candidates, staleMark, onOrderChange, disabled, onOpenFile }: {
  values: string[]
  candidates: string[]
  staleMark: (v: string) => boolean
  onOrderChange: (next: string[]) => void
  disabled: boolean
  onOpenFile?: (name: string) => void
}) {
  const remaining = candidates.filter(c => !values.includes(c))
  return (
    <Space direction="vertical" style={{ width: '100%' }} size={4}>
      {values.map((v, i) => (
        <Space key={v} style={{ display: 'flex' }} size={4}>
          <Button size="small" icon={<ArrowUpOutlined />} disabled={disabled || i === 0} onClick={() => onOrderChange(moveItem(values, i, -1))} />
          <Button size="small" icon={<ArrowDownOutlined />} disabled={disabled || i === values.length - 1} onClick={() => onOrderChange(moveItem(values, i, 1))} />
          {onOpenFile ? (
            <Button type="link" size="small" style={{ padding: 0, fontFamily: 'monospace', fontSize: 13 }}
              onClick={() => onOpenFile(v)}>{v}</Button>
          ) : (
            <span style={{ fontFamily: 'monospace', fontSize: 13 }}>{v}</span>
          )}
          {staleMark(v) && <Tag color="red">stale</Tag>}
          <Button size="small" type="text" danger icon={<CloseOutlined />} disabled={disabled} onClick={() => onOrderChange(values.filter(x => x !== v))} />
        </Space>
      ))}
      <Select
        placeholder="Add from recipe…"
        style={{ width: '100%' }}
        disabled={disabled || remaining.length === 0}
        value={null}
        onChange={(v) => { if (v) onOrderChange([...values, String(v)]) }}
        options={remaining.map(c => ({ value: c, label: c }))}
      />
      {/* §1: `generate new` is the DEFAULT — an empty slot means the pipeline
          generates one, and it contributes nothing to the deploy payload. Shown
          explicitly here so "no selection" reads as a choice, not an oversight. */}
      {values.length === 0 && (
        <Tag color="blue" style={{ width: 'fit-content' }}>generate new — the pipeline will create one</Tag>
      )}
    </Space>
  )
}

export default function FileSetSelection({ selection, candidates, stale, needsEnv, onChange, onOpenFile }: Props) {
  const sel = selection
  const st = stale || {}
  const isStale = (cat: keyof FileSetStale, name: string) => (st[cat] || []).includes(name)
  const set = (patch: Partial<FileSetSelectionData>) => onChange({ ...sel, ...patch })
  const hasAnyStale =
    (st.compose || []).length > 0 || (st.nginx || []).length > 0 ||
    (st.env || []).length > 0 || (st.profiles || []).length > 0

  const nginxOptions = [...new Set([...candidates.nginx, ...(sel.nginx ? [sel.nginx] : [])])].map(c => ({ value: c, label: c }))

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="small">
      {hasAnyStale && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 4 }}
          message="Stale stored selections"
          description="Entries that no longer exist at scan time are kept but flagged missing — they are excluded from generation/deploy requests until re-selected."
        />
      )}

      <div>
        <Typography.Text strong>Compose files</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}> — merge order (first wins, later overrides)</Typography.Text>
        <OrderedMulti
          values={sel.compose}
          candidates={candidates.compose}
          staleMark={(v) => isStale('compose', v)}
          onOrderChange={(next) => set({ compose: next })}
          disabled={false}
          onOpenFile={onOpenFile}
        />
      </div>

      <div>
        <Typography.Text strong>Nginx config</Typography.Text>
        {/* NOT "optional": its absence is as fatal as a missing compose — the
            instance deploys unroutable and the edge 502s the /go/ relay
            (deploy-panel-selection-and-generation-todo.md §1). Leaving it at
            `generate new` means the pipeline generates one. */}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}> — or generate new</Typography.Text>
        <Select
          style={{ width: '100%', marginTop: 4 }}
          placeholder="generate new (none selected)"
          allowClear
          value={sel.nginx}
          onChange={(v) => set({ nginx: v || null })}
          options={nginxOptions}
          optionRender={(o) => (
            <span>
              {o.label}
              {isStale('nginx', o.value as string) && <Tag color="red" style={{ marginLeft: 8 }}>stale</Tag>}
            </span>
          )}
        />
      </div>

      <div>
        <Typography.Text strong>Interpolation env files</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}> — later wins</Typography.Text>
        {needsEnv ? (
          <OrderedMulti
            values={sel.env}
            candidates={candidates.env}
            staleMark={(v) => isStale('env', v)}
            onOrderChange={(next) => set({ env: next })}
            disabled={false}
            onOpenFile={onOpenFile}
          />
        ) : (
          <Alert
            type="info"
            style={{ marginTop: 4 }}
            message="No ${VAR} interpolation detected in the selected compose — env section disabled"
          />
        )}
      </div>

      <div>
        <Typography.Text strong>Compose profiles</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}> — gate optional services</Typography.Text>
        {candidates.profiles.length === 0 ? (
          <div style={{ fontSize: 12, color: '#999', marginTop: 4 }}>
            No non-empty profiles found{sel.compose.length === 0 ? ' — select compose files first' : ''}.
          </div>
        ) : (
          <Select
            mode="multiple"
            allowClear
            placeholder="Select profiles…"
            style={{ width: '100%', marginTop: 4 }}
            value={sel.profiles}
            onChange={(vals: string[]) => set({ profiles: vals.filter(p => !isStale('profiles', p)) })}
            options={candidates.profiles.map(p => ({ value: p, label: p }))}
          />
        )}
        {(st.profiles || []).map(p => (
          <Tag key={p} color="red" style={{ marginTop: 4 }}>{p} — stale</Tag>
        ))}
      </div>
    </Space>
  )
}
