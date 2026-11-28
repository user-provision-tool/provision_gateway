import { Alert, Button, Select, Space, Tag, Tooltip, Typography } from 'antd'
import { ArrowDownOutlined, ArrowUpOutlined, CheckCircleTwoTone, CloseOutlined } from '@ant-design/icons'

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
  // S11 — the env picker's deliberate NO-ENV state, the env counterpart of
  // `serving_ports_none`: "send no env files at all", as distinct from "no file
  // selected yet" (which is the `generate new` state).
  env_none?: boolean
  profiles: string[]
}

export interface FileSetProvenance {
  class: string | null
  legacy?: boolean
  description?: string
  created_at?: string
  sha256?: string | null
  exact?: boolean | null
  deploys?: number | null
  duplicates?: number
  history_unknown?: boolean
}

export interface FileSetCandidates {
  compose: string[]
  nginx: string[]
  stream_conf?: string[]
  mail_conf?: string[]
  serving_ports?: string[]
  env: string[]
  profiles: string[]
  // R4/R5 — the derived evidence keyed by candidate name.
  provenance?: Record<string, FileSetProvenance>
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

export const EMPTY_SELECTION: FileSetSelectionData = { compose: [], nginx: null, stream_conf: null, mail_conf: null, serving_ports: null, env: [], env_none: false, profiles: [] }
export const EMPTY_CANDIDATES: FileSetCandidates = { compose: [], nginx: [], stream_conf: [], mail_conf: [], serving_ports: [], env: [], profiles: [] }

/** The nginx category of a conf file name (G55). The category, not the picker
 * position, decides the slot — and the dashboard's grey-out. */
export function confCategory(name: string): 'http' | 'stream' | 'mail' {
  const base = name.split('/').pop() || name
  if (base.endsWith('.mail.conf')) return 'mail'
  if (base.endsWith('.stream.conf') || (base.includes('.stream.') && base.endsWith('.conf'))) return 'stream'
  return 'http'
}

/** R5.1 — `[exact icon][deploy count] description`, truncated by the picker's
 * EXISTING container width (CSS ellipsis; the width is never changed) with
 * the full text on hover. No provenance degrades to the bare name. */
export function CandidateLabel({ name, p }: { name: string; p?: FileSetProvenance }) {
  if (!p) return <span style={{ fontFamily: 'monospace' }}>{name}</span>
  const deployText = p.deploys == null
    ? (p.history_unknown ? 'history unknown' : (p.legacy ? 'legacy' : null))
    : (p.deploys > 0 ? `deployed ${p.deploys}x` : 'never deployed')
  const full = [
    p.exact ? 'exact match' : null,
    deployText,
    p.duplicates ? `${p.duplicates} duplicate(s)` : null,
    p.description || null,
  ].filter(Boolean).join(' · ') || name
  return (
    <Tooltip title={full}>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, maxWidth: '100%', overflow: 'hidden', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}>
        {p.exact && <CheckCircleTwoTone twoToneColor="#52c41a" style={{ fontSize: 12 }} />}
        <span style={{ fontFamily: 'monospace', flex: '0 0 auto' }}>{name}</span>
        {deployText && <span style={{ color: '#888', fontSize: 11, flex: '0 0 auto' }}>{deployText}</span>}
        {p.description && (
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {p.description}
          </span>
        )}
      </span>
    </Tooltip>
  )
}

export function userVariantFor(name: string): string {
  // R8.1 — a picker edit forks to `<stem>.user.<ext>` with a `user` marker.
  // The tag is single-slotted and idempotent: a name that already carries it
  // is returned as it is. For an env name the family token (base/fix/raw) is
  // REPLACED, so `.env.fix.42` becomes `.env.user.42` rather than growing a
  // second token.
  if (/\.env/.test(name) && !name.includes('.raw.')) {
    const parts = name.split('.')
    if (parts.length > 2 && ['fix', 'base'].includes(parts[1])) {
      parts[1] = 'user'
      return parts.join('.')
    }
    return /\.user$/.test(name) ? name : `${name}.user`
  }
  const m = name.match(/^(.*)\.([^.]+)$/)
  if (!m) return `${name}.user`
  return /\.user$/.test(m[1]) ? name : `${m[1]}.user.${m[2]}`
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
  // G103/T16 — a missing dependency relation forces the env slot to generate
  // new; the env file options are hidden, `None` remains.
  forceEnvGenerate?: boolean
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
/** S11 — the env picker's deliberate-NO-ENV sentinel, the same shape as
 * `SERVING_PORTS_NONE`: "send no env files", not "cleared". */
const ENV_NONE = '__env_none__'

/** T0/R8 — `generate new` is a FIRST-CLASS option inside every picker, compose
 * included: it is the first selectable entry and is the shown value when the
 * slot is empty. It is a sentinel that contributes NOTHING to the deploy
 * payload (choosing it sets the slot back to empty). */
export const GENERATE_NEW = '__generate_new__'
export const GENERATE_NEW_LABEL = 'generate new — the pipeline will create one'

function moveItem(list: string[], idx: number, dir: -1 | 1): string[] {
  const next = [...list]
  const j = idx + dir
  if (j < 0 || j >= next.length) return next
  ;[next[idx], next[j]] = [next[j], next[idx]]
  return next
}

/** The one row shape for a SELECTED file (F1/F2/F6): the name is clickable
 * (when an opener is supplied) and a red cross removes it. Serving ports,
 * nginx and the env-reference list all use THIS component, so the affordances
 * cannot drift apart. A stale selection stays visible, red + struck through. */
export function SelectedFileRow({ name, onOpen, onRemove, stale, disabled }: {
  name: string
  onOpen?: (name: string) => void
  onRemove?: () => void
  stale?: boolean
  disabled?: boolean
}) {
  const mark = stale ? { color: '#cf1322', textDecoration: 'line-through' as const } : {}
  return (
    <Space style={{ display: 'flex', width: '100%' }} size={4}>
      {onOpen ? (
        <Button type="link" size="small"
          style={{ padding: 0, fontFamily: 'monospace', fontSize: 13, ...mark }}
          onClick={() => onOpen(name)}>{name}</Button>
      ) : (
        <span style={{ fontFamily: 'monospace', fontSize: 13, ...mark }}>{name}</span>
      )}
      {onRemove && (
        <Button size="small" type="text" danger icon={<CloseOutlined />}
          disabled={disabled} onClick={onRemove} />
      )}
    </Space>
  )
}

function OrderedMulti({ values, candidates, staleMark, onOrderChange, disabled, onOpenFile, noneSelected, noneLabel, forceGenerate, provenance }: {
  values: string[]
  candidates: string[]
  provenance?: Record<string, FileSetProvenance>
  staleMark: (v: string) => boolean
  // S11 — the second argument carries the deliberate-NO-FILE state with the
  // list, so a caller can apply BOTH in one update (two `set` calls from the
  // same render closure would race and one would overwrite the other).
  onOrderChange: (next: string[], none?: boolean) => void
  disabled: boolean
  onOpenFile?: (name: string) => void
  noneSelected?: boolean
  noneLabel?: string
  // G103/T16 — a missing dependency relation FORCES generate-new: the file
  // options are not offered, while `None` stays selectable.
  forceGenerate?: boolean
}) {
  const remaining = candidates.filter(c => !values.includes(c))
  return (
    <Space direction="vertical" style={{ width: '100%' }} size={4}>
      {values.map((v, i) => (
        <Space key={v} style={{ display: 'flex' }} size={4}>
          <Button size="small" icon={<ArrowUpOutlined />} disabled={disabled || i === 0} onClick={() => onOrderChange(moveItem(values, i, -1), false)} />
          <Button size="small" icon={<ArrowDownOutlined />} disabled={disabled || i === values.length - 1} onClick={() => onOrderChange(moveItem(values, i, 1), false)} />
          {/* A missing selection stays SELECTED but is shown red + struck
              through — no separate alert block. */}
          <SelectedFileRow
            name={v}
            onOpen={onOpenFile}
            stale={staleMark(v)}
            disabled={disabled}
            onRemove={() => onOrderChange(values.filter(x => x !== v), false)}
          />
        </Space>
      ))}
      <Select
        placeholder="Add from recipe…"
        style={{ width: '100%' }}
        disabled={disabled}
        // T0/R8 — the sentinel is the shown value while the slot is empty and
        // is always the first option. Choosing it clears the ordered list; it
        // never enters the selection (or the deploy payload). S11 — an optional
        // `None` entry beside it records the deliberate no-file state.
        value={noneSelected ? ENV_NONE : values.length === 0 ? GENERATE_NEW : null}
        onChange={(v) => {
          if (!v) return
          // None and `generate new` both empty the list; only the flag differs,
          // and both are applied in ONE update.
          if (v === ENV_NONE) { onOrderChange([], true); return }
          if (v === GENERATE_NEW) { onOrderChange([], false); return }
          onOrderChange([...values, String(v)], false)
        }}
        options={[
          { value: GENERATE_NEW, label: GENERATE_NEW_LABEL },
          ...(noneLabel ? [{ value: ENV_NONE, label: noneLabel }] : []),
          ...(forceGenerate ? [] : remaining.map(c => ({
            value: c,
            label: <CandidateLabel name={c} p={provenance?.[c]} />,
          }))),
        ]}
      />
      {/* All options are in the picker; the previous tag beside it is gone. */}
    </Space>
  )
}

export default function FileSetSelection({ selection, candidates, stale, needsEnv, showEnv = true, showProfiles = true, onChange, onOpenFile, forceEnvGenerate = false }: Props) {
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
  // F2 — ADD-MODE. Selected files are rows above the picker, so they are
  // filtered OUT of the dropdown; only the still-open categories are offered
  // (one per category is enforced by disabling the categories already held).
  const nginxOptions = [...new Set([
    ...candidates.nginx,
    ...(candidates.stream_conf || []),
    ...(candidates.mail_conf || []),
  ])].filter(c => !selectedConfs.includes(c)).map(c => ({
    value: c,
    label: staleConf(c) ? <span style={strike}>{c}</span> : (
      <CandidateLabel name={c} p={candidates.provenance?.[c]} />
    ),
    disabled: selectedCats.has(confCategory(c)),
  }))
  const spOptions = [
    // T0/R8 — the sentinel is first, and is the shown value when nothing is
    // selected. R1.3 — `None` remains a deliberate state beside it.
    { value: GENERATE_NEW, label: GENERATE_NEW_LABEL },
    { value: SERVING_PORTS_NONE, label: 'None — proceed without a serving-ports file' },
    ...[...new Set([...(candidates.serving_ports || []), ...(sel.serving_ports ? [sel.serving_ports] : [])])]
      .map(c => ({
        value: c,
        label: isStale('serving_ports', c) ? <span style={strike}>{c}</span> : (
          <CandidateLabel name={c} p={candidates.provenance?.[c]} />
        ),
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
          provenance={candidates.provenance}
          staleMark={(v) => isStale('compose', v)}
          onOrderChange={(next) => set({ compose: next })}
          disabled={false}
          onOpenFile={onOpenFile}
        />
        {/* S18/F3 — profiles are STACKED directly under the compose files they
            derive from, inside the same section (the 640 px modal is too
            cramped for a side-by-side split). The standalone generation panel
            still hides this block (`showProfiles={false}`). */}
        {showProfiles && (
          <div style={{ marginTop: 8 }}>
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
      </div>

      <div>
        <Typography.Text strong>Serving ports</Typography.Text>
        <Typography.Text type="secondary" style={{fontSize:12}}> — declares what can be exposed (compose ports and kinds)</Typography.Text>
        {/* F1 — the selected file is a ROW ABOVE the picker (like compose),
            clickable to edit and clearable with a red cross back to
            `generate new`. A stale selection is still rendered so it can be
            inspected before it is cleared. */}
        {sel.serving_ports && (
          <SelectedFileRow
            name={sel.serving_ports}
            stale={isStale('serving_ports', sel.serving_ports)}
            onOpen={onOpenFile}
            onRemove={() => set({ serving_ports: null, serving_ports_none: false })}
          />
        )}
        <Select
          style={{ width: '100%', marginTop: 4 }}
          placeholder="Select a file…"
          // F1 — the selected file is the row above; the control is the
          // CHOOSER (like the compose add-select), so the value is not shown
          // twice. `None` and `generate new` remain explicit states.
          value={sel.serving_ports_none
            ? SERVING_PORTS_NONE
            : (sel.serving_ports ? undefined : GENERATE_NEW)}
          onChange={(v) => {
            // Three states, all recorded: a file, the deliberate None, and "let
            // the pipeline generate one" (the sentinel — no payload entry).
            if (v === SERVING_PORTS_NONE) set({ serving_ports: null, serving_ports_none: true })
            else if (v === GENERATE_NEW) set({ serving_ports: null, serving_ports_none: false })
            else set({ serving_ports: (v as string) || null, serving_ports_none: false })
          }}
          options={spOptions}
        />
        {/* The sentinel lives in the picker now — no tag beside it. */}
        {/* R1.4/R1.3 — the rule, said where the choice is made. With no file and
            no `None`, the pipeline generates one; with `None` nothing is
            generated, both validations are skipped, and no stream row exists to
            expose — so the card will offer no exposure URL for this instance. */}
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
        {/* F2 — selected confs are ROWS ABOVE the picker, each clickable to
            edit with a per-category red cross. The picker is ADD-MODE: its
            value is the sentinel only while nothing is selected, and a new pick
            MERGES into the other categories rather than resetting them. */}
        {selectedConfs.map(name => (
          <SelectedFileRow
            key={name}
            name={name}
            stale={staleConf(name)}
            onOpen={onOpenFile}
            onRemove={() => {
              const cat = confCategory(name)
              set(cat === 'stream'
                ? { stream_conf: null }
                : cat === 'mail'
                ? { mail_conf: null }
                : { nginx: null })
            }}
          />
        ))}
        {/* The sentinel must LOOK like the sentinel in the other pickers.
            While no conf is selected this control is a SINGLE select, so
            "generate new" renders as plain field text (no tag chip, no dead
            ×); with a conf selected it becomes the ADD-MODE multi-select whose
            value is empty and whose picks MERGE per category. */}
        <Select
          mode={selectedConfs.length === 0 ? undefined : 'multiple'}
          style={{ width: '100%', marginTop: 4 }}
          // T0/R8 — the sentinel is the first option and the shown value while
          // nothing is selected. Choosing it clears every category slot.
          // F2 — with rows above, the control holds only NEW picks, so its
          // value is empty whenever a selection exists.
          value={selectedConfs.length === 0 ? GENERATE_NEW : []}
          placeholder={selectedConfs.length > 0 ? 'Add a config of another category…' : undefined}
          onChange={(vals) => {
            const arr: string[] = Array.isArray(vals)
              ? vals
              : (vals ? [String(vals)] : [])
            const picked = arr.filter((v) => v !== GENERATE_NEW)
            // Choosing the sentinel is the explicit "generate everything"
            // instruction; clearing an already-empty add control changes nothing.
            if (picked.length === 0) {
              if (arr.includes(GENERATE_NEW)) {
                set({ nginx: null, stream_conf: null, mail_conf: null })
              }
              return
            }
            // MERGE, never reset: only the categories of the new picks change.
            // One file per category; a category can never hold two entries.
            const next: Partial<FileSetSelectionData> = {}
            for (const v of picked) {
              if (selectedConfs.includes(v)) continue
              const cat = confCategory(v)
              if (cat === 'stream') next.stream_conf = v
              else if (cat === 'mail') next.mail_conf = v
              else next.nginx = v
            }
            if (Object.keys(next).length > 0) set(next)
          }}
          options={[
            { value: GENERATE_NEW, label: GENERATE_NEW_LABEL },
            ...nginxOptions,
          ]}
          optionRender={(o) => (
            <span>
              {o.label}
              {o.value !== GENERATE_NEW && (
                <Tag color="blue" style={{ marginLeft: 8 }}>{confCategory(o.value as string)}</Tag>
              )}
            </span>
          )}
        />
        {/* The sentinel lives in the picker now — no tag beside it. */}
      </div>

      {showEnv && (
      <div>
        <Typography.Text strong>Env files</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}> — later wins; None sends no env files</Typography.Text>
        {/* S11 — `needs_env:false` is a HINT, not a lock: DeployForm auto-selects
            `None` once for such a recipe, and the operator may still change it. */}
        {!needsEnv && (
          <div style={{ fontSize: 12, color: '#999', marginTop: 4 }}>
            No {'${VAR}'} interpolation detected in the selected compose — None is selected by default; you can still choose a file.
          </div>
        )}
        <OrderedMulti
          values={sel.env}
          candidates={candidates.env}
          provenance={candidates.provenance}
          staleMark={(v) => isStale('env', v)}
          // S11 state machine: a real file clears `env_none`; empty + not None is
          // `generate new`; None empties the list and records the flag.
          onOrderChange={(next, none) => set({ env: next, env_none: !!none })}
          disabled={false}
          onOpenFile={onOpenFile}
          noneSelected={!!sel.env_none}
          noneLabel="None — send no env files"
          forceGenerate={forceEnvGenerate}
        />
      </div>
      )}
    </Space>
  )
}
