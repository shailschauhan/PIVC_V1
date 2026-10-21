import { useState } from 'react'

/* Tiny SVG charts. Colours come from CSS tokens so light/dark both work. */

const DECISION_COLOR = { Accept: 'var(--good)', Reject: 'var(--bad)', 'Needs Review': 'var(--warn)' }
export const DECISIONS = ['Accept', 'Needs Review', 'Reject']

function useTip() {
  const [tip, setTip] = useState(null)
  const show = (e, text) => {
    const r = e.currentTarget.closest('.chart').getBoundingClientRect()
    setTip({ x: e.clientX - r.left, y: e.clientY - r.top, text })
  }
  const node = tip && <div className="tip" style={{ left: tip.x, top: tip.y }}>{tip.text}</div>
  return [node, show, () => setTip(null)]
}

export function Legend({ items }) {
  return (
    <div className="legend">
      {items.map(([label, color]) => (
        <span key={label}><i style={{ background: color }} />{label}</span>
      ))}
    </div>
  )
}

/* Daily volume, stacked by decision. */
export function VolumeChart({ data }) {
  const [tip, show, hide] = useTip()
  if (!data.length) return <p className="muted-text">No analyzed cases in this period.</p>
  const W = 640, H = 200, L = 36, B = 22, T = 8
  const max = Math.max(...data.map((d) => DECISIONS.reduce((s, k) => s + (d[k] || 0), 0)), 1)
  const bw = Math.min(28, (W - L) / data.length - 4)
  const x = (i) => L + (i + 0.5) * ((W - L) / data.length)
  const y = (v) => T + (H - T - B) * (1 - v / max)
  const ticks = [0, Math.ceil(max / 2), max]
  const every = Math.ceil(data.length / 8)
  return (
    <div className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Cases analyzed per day by decision">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={L} x2={W} y1={y(t)} y2={y(t)} className="grid" />
            <text x={L - 6} y={y(t) + 4} textAnchor="end" className="axis">{t}</text>
          </g>
        ))}
        {data.map((d, i) => {
          let acc = 0
          const total = DECISIONS.reduce((s, k) => s + (d[k] || 0), 0)
          return (
            <g key={d.date} onMouseMove={(e) => show(e, `${d.date} · ${total} cases · ` + DECISIONS.map((k) => `${k} ${d[k] || 0}`).join(' · '))} onMouseLeave={hide}>
              <rect x={x(i) - bw / 2 - 2} y={T} width={bw + 4} height={H - T - B} fill="transparent" />
              {DECISIONS.map((k) => {
                const v = d[k] || 0
                if (!v) return null
                const y1 = y(acc + v), h = y(acc) - y1
                acc += v
                return <rect key={k} x={x(i) - bw / 2} y={y1} width={bw} height={Math.max(h - 2, 1)} rx="3" fill={DECISION_COLOR[k]} />
              })}
              {i % every === 0 && <text x={x(i)} y={H - 6} textAnchor="middle" className="axis">{d.date.slice(5)}</text>}
            </g>
          )
        })}
      </svg>
      <Legend items={DECISIONS.map((k) => [k, DECISION_COLOR[k]])} />
      {tip}
    </div>
  )
}

/* Single stacked bar of decision share; segments link through. */
export function DecisionMix({ kpis, onPick }) {
  const total = kpis.accept + kpis.reject + kpis.needs_review
  if (!total) return <p className="muted-text">No decisions yet.</p>
  const rows = [['Accept', kpis.accept], ['Needs Review', kpis.needs_review], ['Reject', kpis.reject]]
  return (
    <div>
      <div className="mix" role="img" aria-label="Decision share">
        {rows.map(([k, n]) => n > 0 && (
          <button key={k} style={{ flex: n, background: DECISION_COLOR[k] }} title={`${k}: ${n}`} onClick={() => onPick(k)} />
        ))}
      </div>
      <table className="mini">
        <tbody>
          {rows.map(([k, n]) => (
            <tr key={k} onClick={() => onPick(k)}>
              <td><i className="dot" style={{ background: DECISION_COLOR[k] }} />{k}</td>
              <td className="num">{n.toLocaleString()}</td>
              <td className="num muted-text">{((n / total) * 100).toFixed(1)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* Histogram over [lo, hi] with optional threshold markers (marker.at is a 0..1 fraction of the axis). */
export function Histogram({ bins, label, markers = [], color = 'var(--brand)', lo = 0, hi = 1, unit = '' }) {
  const [tip, show, hide] = useTip()
  const W = 320, H = 130, B = 20, T = 6
  const n = bins.length
  const max = Math.max(...bins, 1)
  const bw = W / n
  const step = (hi - lo) / n
  const fmt = (v) => (Number.isInteger(v) ? v : +v.toFixed(2))
  return (
    <div className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label}>
        <line x1="0" x2={W} y1={H - B} y2={H - B} className="grid" />
        {bins.map((c, i) => {
          const h = ((H - B - T) * c) / max
          return (
            <g key={i} onMouseMove={(e) => show(e, `${fmt(lo + i * step)}–${fmt(lo + (i + 1) * step)}${unit ? ' ' + unit : ''}: ${c.toLocaleString()} videos`)} onMouseLeave={hide}>
              <rect x={i * bw} y={T} width={bw} height={H - B - T} fill="transparent" />
              {c > 0 && <rect x={i * bw + 2} y={H - B - h} width={bw - 4} height={h} rx="3" fill={color} />}
            </g>
          )
        })}
        {markers.filter((m) => m.at >= 0 && m.at <= 1).map((m) => (
          <g key={m.label}>
            <line x1={m.at * W} x2={m.at * W} y1={T} y2={H - B} className="marker" />
            <text x={Math.min(m.at * W + 3, W - 30)} y={T + 9} className="axis">{m.label}</text>
          </g>
        ))}
        {[0, 0.5, 1].map((t) => <text key={t} x={t * W} y={H - 5} textAnchor={t === 0 ? 'start' : t === 1 ? 'end' : 'middle'} className="axis">{fmt(lo + t * (hi - lo))}</text>)}
      </svg>
      {tip}
    </div>
  )
}

const TONE = { good: 'var(--good)', warn: 'var(--warn)', bad: 'var(--bad)', muted: 'var(--muted)' }
export const toneColor = (t) => TONE[t] || TONE.muted

/* One stacked bar of a test's outcomes; segments are buttons that drill into the matching videos. */
export function OutcomeBar({ outcomes, total, onPick }) {
  if (!total) return <div className="mix empty-mix" aria-label="No results yet" />
  return (
    <div className="mix" role="img" aria-label="Outcome share">
      {outcomes.filter((o) => o.count > 0).map((o) => (
        <button key={o.status} style={{ flex: o.count, background: toneColor(o.tone), opacity: o.tone === 'muted' ? 0.55 : 1 }}
          title={`${o.status}: ${o.count.toLocaleString()} (${((o.count / total) * 100).toFixed(1)}%)`} onClick={() => onPick(o)} />
      ))}
    </div>
  )
}

/* Horizontal rate bars (share of cases flagged). */
export function RateBars({ rows, onPick }) {
  return (
    <div className="rates">
      {rows.map((r) => (
        <button key={r.key} className="rate" onClick={() => onPick(r)} title={r.title}>
          <span className="rate-label">{r.label}{r.tag && <em>{r.tag}</em>}</span>
          <span className="rate-track"><span style={{ width: `${Math.max(r.rate * 100, r.rate > 0 ? 1 : 0)}%` }} /></span>
          <span className="rate-val">{(r.rate * 100).toFixed(1)}%</span>
          <span className="rate-n muted-text">{r.flagged.toLocaleString()}</span>
        </button>
      ))}
    </div>
  )
}
