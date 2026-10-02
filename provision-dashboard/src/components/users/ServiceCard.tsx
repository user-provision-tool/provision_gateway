import { ReactNode } from 'react'
import { Collapse, Space } from 'antd'

// See the note at the Panel usage below.
const Panel = Collapse.Panel as any

/**
 * The ONE service card widget (GRILLING G24).
 *
 * A pre-deploy session and a registered instance are the same thing at
 * different points in the path, so they render through the SAME widget and
 * differ only by STATUS. Before this they were two unrelated pieces of markup —
 * a `Collapse.Panel` for instances and a bespoke `<Card>` of `<Space>` rows for
 * sessions — and every divergence between them (duplicate `Check`/`Retry`
 * behaviour, a `Cancel` that could not lie about a job it did not have) came
 * out of that split.
 *
 * Two rules are structural here, not conventions:
 *
 * 1. **The alert lives on the HEADER, IN the title row.** Anything passed as
 *    `alert` renders in the panel's header, so it is visible whether the card is
 *    folded or expanded. An alert in the body is invisible exactly when the
 *    operator is scanning the list, which is when it matters. It sits INLINE,
 *    directly after the service name, and it must be **ONE LINE**: a block alert
 *    (or two stacked ones) grew the header to ~150 px against ~53 px for a plain
 *    card, which reflowed the whole list every time a card had something to say.
 *    Callers therefore pass a single inline element — a coloured `Text` with an
 *    optional `Tooltip` carrying the long-form remedy — never an antd Alert
 *    block.
 * 2. **The body is the caller's.** Status-specific detail goes in `children`;
 *    status-specific ACTIONS go in `actions`, which is what lets a pre-deploy
 *    card suppress the instance-only buttons (`Rebuild`, `pause/play`,
 *    `Redeploy`, `key`, `Dup`) instead of inheriting them.
 */
export interface ServiceCardProps {
  /**
   * The panel's identity, and it MUST also be the React `key` the caller gives
   * this component: AntD derives the panel key from the element's React key, so
   * the two are the same string by construction, and `activeKey` (the page's
   * `expandedPanels`) is written in those terms.
   */
  panelKey: string
  /** The status badge — the ONLY thing that must differ between card kinds. */
  badge: ReactNode
  title: ReactNode
  /** The label tag's text. */
  label: ReactNode
  /** Optional row after the title (CPU/RAM tags, resource stats, ...). */
  headerExtra?: ReactNode
  /** A ONE-LINE status note, inline after the title — see rule 1. */
  alert?: ReactNode
  /** The header's right-hand buttons. */
  actions?: ReactNode
  children?: ReactNode
}

export default function ServiceCard({
  panelKey, badge, title, label, headerExtra, alert, actions, children,
  ...rest
}: ServiceCardProps) {
  // `...rest` is LOAD-BEARING, not tidiness. AntD renders a `Collapse`'s
  // children by CLONING each one with the props that actually drive the panel
  // (`isActive`, `panelKey`, `onItemClick`, `prefixCls`, `expandIcon`,
  // `collapsible`, …) — it does not care that the child is this wrapper rather
  // than a `Collapse.Panel`, and it passes the content it was handed as
  // `children`. A wrapper that accepts those props and drops them leaves the
  // inner Panel permanently inactive: every card renders, none opens, and the
  // toggle looks broken. Found live in Round 1 of this goal — the instance
  // cards stopped expanding too, which is how the wrapper (not the session
  // markup) was identified as the cause.
  return (
    // `Panel` is untyped on purpose. The props that make this work —
    // `panelKey` above all — are rc-collapse's INTERNAL contract and are not in
    // antd's published `CollapsePanelProps`, even though antd's Panel consumes
    // them. `panelKey` is re-stated because it is destructured out of `rest`:
    // the Panel reports its identity back through `onItemClick(panelKey)`, so
    // dropping it sends `undefined` into the page's `expandedPanels`, and the
    // key-prefix effect then calls `parseServiceKey(undefined)` and crashes the
    // render.
    <Panel
      {...rest}
      key={panelKey}
      panelKey={panelKey}
      header={
        // ONE row: the badge, the service name, then the status note INLINE.
        // The note used to be a block on a line of its own (and, on a failed
        // instance, up to two stacked ones), which made an alerted card's header
        // ~3x a plain card's and reflowed the list as states changed.
        <div style={{ display: 'flex', alignItems: 'center', minWidth: 0 }}>
          <Space>
            {badge}
            {title}
            {alert}
            {label}
            {headerExtra}
          </Space>
        </div>
      }
      extra={
        <Space onClick={e => e.stopPropagation()}>
          {actions}
        </Space>
      }
    >
      {children}
    </Panel>
  )
}
