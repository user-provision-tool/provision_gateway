import { Alert, Button, Select, Space, Tag, Typography } from 'antd'
import { ArrowDownOutlined, ArrowUpOutlined, CloseOutlined } from '@ant-design/icons'

/**
 * Per-recipe base-file selection UI — shared by the LLM generate-missing
 * panel and the deploy panel (design §Selection & UI).
 *
 * - compose: ordered multi (merge order is user-visible)
 * - serving_ports: single, with the deliberate `None` state beside it (R1.3) —
 *   the artifact that declares what may be exposed, and the one the deploy must
 *   carry for a stream conf to be produced at all (T15)
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
  // R1.1: the serving-ports artifact, single-valued like nginx. `null` means "no
  // file selected" (the pipeline generates one); the deliberate NO-FILE state is
  // recorded separately as `serving_ports_none`, so "deliberately none" and "no
  // file exists" stay distinguishable (R1.3).
  serving_ports?: string | null
  serving_ports_none?: boolean
  env: string[]
  profiles: string[]
}

export interface FileSetCandidates {
  compose: string[]
  nginx: string[]
  serving_ports?: string[]
  env: string[]
  profiles: string[]
}

export interface FileSetStale {
  compose?: string[]
  nginx?: string[]
  serving_ports?: string[]
  env?: string[]
  profiles?: string[]
}

export const EMPTY_SELECTION: FileSetSelectionData = { compose: [], nginx: null, serving_ports: null, env: [], profiles: [] }
export const EMPTY_CANDIDATES: FileSetCandidates = { compose: [], nginx: [], serving_ports: [], env: [], profiles: [] }

export function fixVariantFor(name: string): string {
  // Mirrors the gateway's FIX_FILENAME: the tag goes before the extension and
  // is single-slotted (overwritten each round, never accumulated).
  //
  // R4-F66 — and this function did NOT honour its own rule on a name that is
  // ALREADY a fork: `serving_ports.base.362.fix.yml` became
  // `serving_ports.base.362.fix.fix.yml`. It bites the moment a fork is opened
  // and saved again — which is the ordinary second edit of an artifact the
  // operator has already touched once, and also what the new `primary` picker
  // does when the picker is already pointing at a fork. Single-slotted means
  // idempotent: a name that carries the tag is returned as it is.
  if (/\.env/.test(name) && !name.includes('.raw.')) {
    const stem = name.split('.').slice(0, -1).join('.') || '.env'
    return /\.fix$/.test(stem) ? name : `${stem}.fix`
  }
  const m = name.match(/^(.*)\.([^.]+)$/)
  if (!m) return `${name}.fix`
  return /\.fix$/.test(m[1]) ? name : `${m[1]}.fix.${m[2]}`
}

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
  // R1.3 — the `primary` row of the serving-ports artifact (the service the
  // instance hostname points at). The rows are read by the CALLER, which owns
  // the file fetch; this component only renders the choice.
  spRows?: ServingPortsRow[]
  onPrimaryChange?: (subService: string) => void
}

/** One row of a serving-ports artifact, as the picker needs it.
 *
 * Keyed by sub-service AND port, not by sub-service alone: an artifact may
 * declare one sub-service on two rows — neo4j's Browser (7474, http) and its
 * Bolt port (7687, stream) are the worked example — and "which row is primary"
 * is a question about a ROW. Keyed by name alone the picker would offer the
 * same entry twice and an edit would mark both.
 */
export interface ServingPortsRow { subService: string; port: string; primary: boolean }

/** The identity of a row, for the picker's value and for `setPrimary`. */
export const servingPortsKey = (r: ServingPortsRow): string => `${r.subService}:${r.port}`

/**
 * Read a serving-ports artifact's rows — enough for the `primary` picker, and
 * nothing more. A line scan rather than a YAML parse on purpose: this runs in
 * the browser, the file is small and machine-shaped, and a parse/serialize
 * round-trip would rewrite the operator's comments and formatting.
 */
export function servingPortsRows(content: string): ServingPortsRow[] {
  const rows: ServingPortsRow[] = []
  let cur: ServingPortsRow | null = null
  for (const line of content.split('\n')) {
    const m = /^\s*-\s*sub_service:\s*(.+?)\s*$/.exec(line)
    if (m) {
      cur = { subService: m[1].replace(/^['"]|['"]$/g, ''), port: '', primary: false }
      rows.push(cur)
      continue
    }
    if (!cur) continue
    const pm = /^\s*port:\s*(\S+)\s*$/.exec(line)
    if (pm) cur.port = pm[1].replace(/^['"]|['"]$/g, '')
    if (/^\s*primary:\s*true\s*$/.test(line)) cur.primary = true
  }
  return rows
}

/**
 * Move `primary: true` to *subService*, preserving every other line byte for
 * byte — comments included. R1.3: "any row edit, `primary` included, forks
 * `.fix` and flips the picker to the fork"; this is the edit, the caller does
 * the fork.
 *
 * The chosen row keeps its own line (its value replaced); every other row's
 * `primary:` line is dropped; a chosen row that has none gets one after its
 * `port:` line, at that line's own indentation.
 */
export function setPrimary(content: string, key: string): string {
  const [wantSub, wantPort] = key.split(':')
  const lines = content.split('\n')
  const isRow = (line: string) => /^\s*-\s*sub_service:\s*(.+?)\s*$/.test(line)
  const subOf = (line: string) => (line.match(/^\s*-\s*sub_service:\s*(.+?)\s*$/) || [])[1]?.replace(/^['"]|['"]$/g, '') || ''
  const portOf = (line: string) => (line.match(/^\s*port:\s*(\S+)\s*$/) || [])[1]?.replace(/^['"]|['"]$/g, '') || ''

  // Walk the rows once to learn the chosen row's identity and whether it
  // already carries a `primary:` line of its own.
  let curSub = '', curPort = '', hasLine = false, curIsChosen = false
  for (const line of lines) {
    if (isRow(line)) { curSub = subOf(line); curPort = ''; curIsChosen = curSub === wantSub && (wantPort === '' || wantPort === undefined) }
    else if (curSub) { const p = portOf(line); if (p) curPort = p }
    if (curSub && wantPort !== '' && wantPort !== undefined && curPort) curIsChosen = curSub === wantSub && curPort === wantPort
    if (curIsChosen && /^\s*primary:\s*/.test(line)) hasLine = true
  }

  const out: string[] = []
  curSub = ''; curPort = ''; curIsChosen = false
  let inserted = false
  for (const line of lines) {
    if (isRow(line)) { curSub = subOf(line); curPort = ''; curIsChosen = curSub === wantSub && (wantPort === '' || wantPort === undefined) }
    else if (curSub) { const p = portOf(line); if (p) { curPort = p; if (wantPort !== '' && wantPort !== undefined) curIsChosen = curSub === wantSub && curPort === wantPort } }
    if (curSub && /^(\s*)primary:\s*/.test(line)) {
      if (curIsChosen) out.push(line.replace(/primary:\s*\S+/, 'primary: true'))
      continue                       // every other row loses its primary line
    }
    out.push(line)
    if (!hasLine && !inserted && curIsChosen && /^(\s*)port:\s*/.test(line)) {
      out.push(`${/^(\s*)/.exec(line)![1]}primary: true`)
      inserted = true
    }
  }
  return out.join('\n')
}

export function phaseOfFile(filename: string): string {
  const base = filename.split('/').pop() || filename
  if (base.endsWith('.j2')) return phaseOfFile(base.slice(0, -3))
  if (base.startsWith('docker-compose') || base.startsWith('compose')) return 'compose'
  // T14 / R1.3 — the serving-ports artifact, matched by the platform's own
  // candidate predicate (`serving_ports*` / `serving-ports*`), never by an exact
  // name: a generated one carries the job token. Without this case the file
  // mapped to '' and both callers — `ok = !v || v.valid` — rendered it green
  // whatever its contents (R2-F7). `validate_ports` is the key the gateway
  // records that step's result under.
  if (base.startsWith('serving_ports') || base.startsWith('serving-ports')) return 'validate_ports'
  if (base.endsWith('.conf')) return 'nginx'
  if (base.startsWith('.env')) return 'env'
  return ''
}

/** The picker's deliberate-NO-FILE option (R1.3). A sentinel, not `null`: antd's
 * `Select` cannot distinguish "cleared" from "chose the None entry", and the two
 * mean different things — cleared returns to "the pipeline generates one". */
const SERVING_PORTS_NONE = '__none__'

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

export default function FileSetSelection({ selection, candidates, stale, needsEnv, onChange, onOpenFile, spRows, onPrimaryChange }: Props) {
  const sel = selection
  const st = stale || {}
  const isStale = (cat: keyof FileSetStale, name: string) => (st[cat] || []).includes(name)
  const set = (patch: Partial<FileSetSelectionData>) => onChange({ ...sel, ...patch })
  const hasAnyStale =
    (st.compose || []).length > 0 || (st.nginx || []).length > 0 ||
    (st.serving_ports || []).length > 0 ||
    (st.env || []).length > 0 || (st.profiles || []).length > 0

  const nginxOptions = [...new Set([...candidates.nginx, ...(sel.nginx ? [sel.nginx] : [])])].map(c => ({ value: c, label: c }))
  const spOptions = [
    // R1.3 — `None` sits in the picker itself, as the user ruled it: an operator
    // choosing it means "proceed with no serving-ports file", which suppresses
    // generation and both validations and exposes nothing extra.
    { value: SERVING_PORTS_NONE, label: 'None — proceed without a serving-ports file' },
    ...[...new Set([...(candidates.serving_ports || []), ...(sel.serving_ports ? [sel.serving_ports] : [])])]
      .map(c => ({ value: c, label: c })),
  ]

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
        <Typography.Text strong>Serving ports</Typography.Text>
        <Typography.Text type="secondary" style={{fontSize:12}}> — declares what can be exposed (compose ports and kinds)</Typography.Text>
        <Select
          style={{ width: '100%', marginTop: 4 }}
          placeholder="generate new (none selected)"
          allowClear
          value={sel.serving_ports_none ? SERVING_PORTS_NONE : (sel.serving_ports || undefined)}
          onChange={(v) => {
            // Three states, all recorded: a file, the deliberate None, and "let
            // the pipeline generate one" (cleared).
            if (v === SERVING_PORTS_NONE) set({ serving_ports: null, serving_ports_none: true })
            else set({ serving_ports: (v as string) || null, serving_ports_none: false })
          }}
          options={spOptions}
          optionRender={(o) => (
            <span>
              {o.label}
              {isStale('serving_ports', o.value as string) && <Tag color="red" style={{ marginLeft: 8 }}>stale</Tag>}
            </span>
          )}
        />
        {/* R1.4/R1.3 — the rule, said where the choice is made. With no file and
            no `None`, the pipeline generates one; with `None` nothing is
            generated, both validations are skipped, and no stream row exists to
            expose — so the card will offer no exposure URL for this instance. */}
        {/* R1.3 — the file itself, openable for view/edit. Saving forks into the
            `.fix` variant and flips this picker (the same affordance the multi
            categories have); without it the serving-ports artifact was the ONE
            category that could not be edited from here at all. */}
        {sel.serving_ports && onOpenFile && (
          <div style={{ marginTop: 4 }}>
            <Button type="link" size="small" style={{ padding: 0, fontFamily: 'monospace', fontSize: 13 }}
              onClick={() => onOpenFile(sel.serving_ports as string)}>{sel.serving_ports}</Button>
          </div>
        )}
        {/* R1.3 — `primary`, "between the serving-ports and nginx pickers", and
            only when a ports file is in play: it is a field OF that file. */}
        {sel.serving_ports && (spRows || []).length > 0 && (
          <div style={{ marginTop: 6 }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              primary — the service the instance hostname points at
            </Typography.Text>
            <Select
              style={{ width: '100%', marginTop: 2 }}
              value={(() => { const p = (spRows || []).find(r => r.primary); return p ? servingPortsKey(p) : undefined })()}
              placeholder="(no primary declared)"
              onChange={(v) => onPrimaryChange?.(String(v))}
              options={(spRows || []).map(r => ({ value: servingPortsKey(r), label: r.port ? `${r.subService}  (${r.port})` : r.subService }))}
            />
          </div>
        )}
        {sel.serving_ports_none && (
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: 6 }}
            message="No serving-ports file — nothing can be exposed"
            description="The generation is suppressed and both validations are skipped; a stream row therefore has no conf, no route and no exposure URL on the card."
          />
        )}
        {!sel.serving_ports_none && sel.serving_ports && isStale('serving_ports', sel.serving_ports) && (
          <Tag color="red" style={{ marginTop: 6 }}>{sel.serving_ports} — stale</Tag>
        )}
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
