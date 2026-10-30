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
  // G55 — ONE file per category. `nginx` is the HTTP category (the stored key
  // keeps its old name for compatibility); stream and mail have their own
  // slots. The PICKER is one multi-select over all three.
  nginx: string | null
  stream_conf?: string | null
  mail_conf?: string | null
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
  stream_conf?: string[]
  mail_conf?: string[]
  serving_ports?: string[]
  env: string[]
  profiles: string[]
}

export interface FileSetStale {
  compose?: string[]
  nginx?: string[]
  stream_conf?: string[]
  mail_conf?: string[]
  serving_ports?: string[]
  env?: string[]
  profiles?: string[]
}

export const EMPTY_SELECTION: FileSetSelectionData = { compose: [], nginx: null, stream_conf: null, mail_conf: null, serving_ports: null, env: [], profiles: [] }
export const EMPTY_CANDIDATES: FileSetCandidates = { compose: [], nginx: [], stream_conf: [], mail_conf: [], serving_ports: [], env: [], profiles: [] }

/** The nginx category of a conf file name (G55). The category, not the picker
 * position, decides the slot — and the dashboard's grey-out. */
export function confCategory(name: string): 'http' | 'stream' | 'mail' {
  const base = name.split('/').pop() || name
  if (base.endsWith('.mail.conf')) return 'mail'
  if (base.endsWith('.stream.conf') || (base.includes('.stream.') && base.endsWith('.conf'))) return 'stream'
  return 'http'
}

export function fixVariantFor(name: string): string {
  // Mirrors the gateway's FIX_FILENAME: the tag goes before the extension and
  // is single-slotted (overwritten each round, never accumulated).
  //
  // R4-F66 — and this function did NOT honour its own rule on a name that is
  // ALREADY a fork: `serving_ports.base.362.fix.yml` became
  // `serving_ports.base.362.fix.fix.yml`. It bites the moment a fork is opened
  // and saved again — which is the ordinary second edit of an artifact the
  // operator has already touched once. Single-slotted means idempotent: a name
  // that carries the tag is returned as it is.
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
  // The standalone Generate Base Files panel has no interpolation env slot;
  // it supplies an Env reference picker instead.
  showEnv?: boolean
  // Profiles are a DEPLOY-time activation control; a recipe-level generation
  // must be profile-blind (the -api filters blocks at render), so the
  // standalone panel hides this section.
  showProfiles?: boolean
  onChange: (next: FileSetSelectionData) => void
  // §5b: clicking a SELECTED file's name opens it for view/edit. Saving forks
  // into the `.fix` variant rather than writing back over the selection, so a
  // repo-original is never rewritten by an edit made here.
  onOpenFile?: (name: string) => void
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
          {/* A missing selection stays SELECTED but is shown red + struck
              through — no separate alert block. */}
          {onOpenFile ? (
            <Button type="link" size="small"
              style={{ padding: 0, fontFamily: 'monospace', fontSize: 13,
                ...(staleMark(v) ? { color: '#cf1322', textDecoration: 'line-through' } : {}) }}
              onClick={() => onOpenFile(v)}>{v}</Button>
          ) : (
            <span style={{ fontFamily: 'monospace', fontSize: 13,
              ...(staleMark(v) ? { color: '#cf1322', textDecoration: 'line-through' } : {}) }}>{v}</span>
          )}
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

export default function FileSetSelection({ selection, candidates, stale, needsEnv, showEnv = true, showProfiles = true, onChange, onOpenFile }: Props) {
  const sel = selection
  const st = stale || {}
  const isStale = (cat: keyof FileSetStale, name: string) => (st[cat] || []).includes(name)
  const set = (patch: Partial<FileSetSelectionData>) => onChange({ ...sel, ...patch })

  // G55 — ONE multi-select over every conf category. Selecting one file of a
  // category disables the OTHER candidates of that same category; files of the
  // other categories stay selectable. At most one per category.
  const selectedConfs = [sel.nginx, sel.stream_conf, sel.mail_conf].filter(Boolean) as string[]
  const selectedCats = new Set(selectedConfs.map(confCategory))
  const staleConf = (name: string) =>
    (st.nginx || []).includes(name) || (st.stream_conf || []).includes(name) ||
    (st.mail_conf || []).includes(name)
  // A stale name stays readable as SELECTED, in red with a line through it.
  // The label is a node so both the dropdown AND the selected tag render it.
  const strike = { color: '#cf1322', textDecoration: 'line-through' as const }
  const nginxOptions = [...new Set([
    ...candidates.nginx,
    ...(candidates.stream_conf || []),
    ...(candidates.mail_conf || []),
    ...selectedConfs,
  ])].map(c => ({
    value: c,
    label: staleConf(c) ? <span style={strike}>{c}</span> : c,
    disabled: selectedCats.has(confCategory(c)) && !selectedConfs.includes(c),
  }))
  const spOptions = [
    // R1.3 — `None` sits in the picker itself, as the user ruled it: an operator
    // choosing it means "proceed with no serving-ports file", which suppresses
    // generation and both validations and exposes nothing extra.
    { value: SERVING_PORTS_NONE, label: 'None — proceed without a serving-ports file' },
    ...[...new Set([...(candidates.serving_ports || []), ...(sel.serving_ports ? [sel.serving_ports] : [])])]
      .map(c => ({
        value: c,
        label: isStale('serving_ports', c) ? <span style={strike}>{c}</span> : c,
      })),
  ]

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="small">
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
        />
        {!sel.serving_ports && !sel.serving_ports_none && (
          <Tag color="blue" style={{ width: 'fit-content', marginTop: 4 }}>
            generate new — the pipeline will create one
          </Tag>
        )}
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
            <Button type="link" size="small"
              style={{ padding: 0, fontFamily: 'monospace', fontSize: 13,
                ...(isStale('serving_ports', sel.serving_ports) ? strike : {}) }}
              onClick={() => onOpenFile(sel.serving_ports as string)}>{sel.serving_ports}</Button>
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
        {/* A stale selection reads red + struck through in place — no alert,
            no extra tag row. */}
      </div>

      <div>
        <Typography.Text strong>Nginx config</Typography.Text>
        {/* NOT "optional": its absence is as fatal as a missing compose — the
            instance deploys unroutable and the edge 502s the /go/ relay
            (deploy-panel-selection-and-generation-todo.md §1). Leaving it at
            `generate new` means the pipeline generates one. */}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}> — or generate new</Typography.Text>
        <Select
          mode="multiple"
          style={{ width: '100%', marginTop: 4 }}
          placeholder="select one per category, or generate new"
          allowClear
          value={selectedConfs}
          onChange={(vals: string[]) => {
            // One file per category; a category can never hold two entries.
            const next: Partial<FileSetSelectionData> = {
              nginx: null, stream_conf: null, mail_conf: null,
            }
            for (const v of vals) {
              const cat = confCategory(v)
              if (cat === 'stream') next.stream_conf = v
              else if (cat === 'mail') next.mail_conf = v
              else next.nginx = v
            }
            set(next)
          }}
          options={nginxOptions}
          optionRender={(o) => (
            <span>
              {o.label}
              <Tag color="blue" style={{ marginLeft: 8 }}>{confCategory(o.value as string)}</Tag>
            </span>
          )}
        />
        {selectedConfs.length === 0 && (
          <Tag color="blue" style={{ width: 'fit-content', marginTop: 4 }}>
            generate new — the pipeline will create one
          </Tag>
        )}
      </div>

      {showEnv && (
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
      )}

      {showProfiles && (
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
          <span key={p} style={{ marginTop: 4, display: 'inline-block', ...strike }}>{p}</span>
        ))}
      </div>
      )}
    </Space>
  )
}
