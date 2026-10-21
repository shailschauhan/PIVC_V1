import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Histogram, toneColor } from './charts.jsx'

/* ---------- formatting ---------- */
export const n = (v) => (v ?? 0).toLocaleString()
export const pct = (v, d = 0) => (v == null ? '—' : `${(v * 100).toFixed(d)}%`)
export const hoursSince = (iso) => (Date.now() - new Date(iso).getTime()) / 36e5
export function fmtHours(h) {
  if (h == null) return '—'
  if (h < 1) return `${Math.max(1, Math.round(h * 60))} min`
  if (h < 48) return `${h < 10 ? h.toFixed(1) : Math.round(h)} h`
  return `${Math.round(h / 24)} d`
}
export function fmtMs(ms) {
  if (ms == null) return '—'
  if (ms >= 60000) return `${(ms / 60000).toFixed(1)} min`
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`
}
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const dayLabel = (t) => { const d = new Date(`${t}T00:00:00Z`); return `${d.getUTCDate()} ${MON[d.getUTCMonth()]}` }
const hourLabel = (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })

/* ---------- small pieces ---------- */
/* Change versus the previous period. `good` says which direction is good news: 'up' | 'down' | 'none'. */
export function Delta({ cur, prev, kind = 'pct', good = 'none', label = 'vs the previous period' }) {
  if (cur == null || prev == null) return null
  let diff, text
  if (kind === 'pts') { diff = (cur - prev) * 100; text = `${Math.abs(diff).toFixed(1)} pts` }
  else if (kind === 'abs') { diff = cur - prev; text = fmtMs(Math.abs(diff)) }
  else {
    if (prev === 0) return cur > 0 ? <span className="delta flat" title={label}>new</span> : null
    diff = ((cur - prev) / prev) * 100; text = `${Math.abs(diff).toFixed(0)}%`
  }
  const flat = Math.abs(diff) < (kind === 'pts' ? 0.5 : kind === 'abs' ? 500 : 1)
  const up = diff > 0
  const tone = flat || good === 'none' ? 'flat' : (up === (good === 'up') ? 'good' : 'bad')
  return (
    <span className={`delta ${tone}`} title={`${up ? 'Up' : 'Down'} ${text} ${label}`}>
      <span aria-hidden="true">{flat ? '•' : up ? '▲' : '▼'}</span>
      <span className="sr-only">{up ? 'up' : 'down'}</span> {flat ? 'steady' : text}
    </span>
  )
}

export function Sparkline({ values, width = 92, height = 30 }) {
  if (!values?.length || Math.max(...values) === 0) return null
  const max = Math.max(...values)
  const step = width / Math.max(values.length - 1, 1)
  const pts = values.map((v, i) => [i * step, height - 2 - (v / max) * (height - 6)])
  const line = pts.map((p) => p.join(',')).join(' ')
  return (
    <svg className="spark-kpi" width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <polygon points={`0,${height} ${line} ${width},${height}`} fill="var(--brand)" opacity="0.12" />
      <polyline points={line} fill="none" stroke="var(--brand)" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

export function Kpi({ label, value, unit, delta, hint, spark, to, tone }) {
  const body = (
    <>
      <div className="kpi-label">{label}</div>
      <div className="kpi-main">
        <div className="kpi-value">{value}{unit && <small>{unit}</small>}</div>
        {spark}
      </div>
      <div className="kpi-foot">{delta}{hint && <span className="kpi-hint">{hint}</span>}</div>
    </>
  )
  return to ? <Link className={`kpi ${tone || ''}`} to={to}>{body}</Link> : <div className={`kpi ${tone || ''}`}>{body}</div>
}

export function Card({ title, sub, action, children, flush, className = '' }) {
  return (
    <section className={`card dcard ${flush ? 'flush' : ''} ${className}`}>
      {(title || action) && (
        <header className="dcard-head">
          <div><h3>{title}</h3>{sub && <p className="sub">{sub}</p>}</div>
          {action}
        </header>
      )}
      {children}
    </section>
  )
}

/* ---------- system status ---------- */
export function SystemStrip({ engines, canConfigure }) {
  const down = engines.filter((e) => !e.ready)
  return (
    <div className="sysstrip" role="status" aria-label="System status">
      <span className="sys-title">Systems</span>
      {engines.map((e) => {
        const pill = (
          <span className={`pill ${e.ready ? 'ok' : e.key === 'video_authenticity' ? 'bad' : 'warn'}`} title={e.ready ? `${e.label}: ready` : `${e.label}: ${e.reason}`}>
            <i aria-hidden="true" />{e.label}{!e.ready && <em>offline</em>}
          </span>
        )
        return e.ready || !canConfigure ? <span key={e.key}>{pill}</span> : <Link key={e.key} to="/health" className="pill-link">{pill}</Link>
      })}
      <span className="sys-summary">{down.length ? `${down.length} of ${engines.length} unavailable` : 'All systems ready'}</span>
    </div>
  )
}

/* ---------- needs attention ---------- */
const SEV = { critical: 0, warning: 1, info: 2 }

export function attentionItems(d, canConfigure) {
  const items = []
  const k = d.kpis, b = d.backlog, sla = d.sla_hours
  if (b.pending > 0) {
    const oldest = b.oldest[0] ? hoursSince(b.oldest[0].since) : 0
    const sev = b.over_sla > 0 ? 'critical' : oldest > sla * 0.5 ? 'warning' : 'info'
    items.push({ sev, key: 'backlog', title: `${n(b.pending)} video${b.pending === 1 ? ' is' : 's are'} waiting for a human decision`,
      detail: `The oldest has waited ${fmtHours(oldest)}${b.over_sla ? ` · ${n(b.over_sla)} over the ${sla} h target` : ` · target ${sla} h`}`, to: '/review', cta: 'Open review queue' })
  }
  if (k.failed > 0) {
    items.push({ sev: 'warning', key: 'failed', title: `${n(k.failed)} video${k.failed === 1 ? '' : 's'} failed to analyze`,
      detail: 'Re-run them from All videos once the cause is fixed', to: '/cases?status=FAILED', cta: 'View failed videos' })
  }
  for (const e of d.engines.filter((x) => !x.ready)) {
    items.push({ sev: e.key === 'video_authenticity' ? 'critical' : 'warning', key: `eng-${e.key}`, title: `${e.label} is not available`,
      detail: `${e.reason}. ${e.key === 'video_authenticity' ? 'Uploads are blocked.' : 'That test is reported as Unavailable; the others still run.'}`,
      ...(canConfigure ? { to: `/health/${e.key}`, cta: 'See service health' } : {}) })
  }
  if (k.queue >= 20) {
    items.push({ sev: 'info', key: 'queue', title: `${n(k.queue)} videos are queued for analysis`, detail: 'They are processed one after another; new results will appear as they finish.' })
  }
  const prev = d.previous?.tests || {}
  const worst = [...d.tests].filter((t) => t.total >= 20 && (t.attention_rate ?? 0) >= 0.15).sort((a, b) => b.attention_rate - a.attention_rate)[0]
  if (worst) {
    const top = worst.outcomes.filter((o) => o.tone === 'warn' || o.tone === 'bad').sort((a, b) => b.count - a.count)[0]
    const p = prev[worst.key]?.attention_rate
    items.push({ sev: 'warning', key: `test-${worst.key}`, title: `${worst.label}: ${pct(worst.attention_rate)} of videos need a person to look`,
      detail: `Mostly “${top.status}” (${n(top.count)})${p != null ? ` · ${worst.attention_rate > p ? 'up' : 'down'} from ${pct(p)} in the previous period` : ''}`,
      to: `/cases?parameter=${worst.key}&parameter_status=${encodeURIComponent(top.status)}&days=${d.days}`, cta: 'See these videos' })
  }
  if ((k.overturn_rate ?? 0) > 0.1 && k.auto_decided >= 20) {
    items.push({ sev: 'warning', key: 'overturn', title: `Reviewers overturned ${pct(k.overturn_rate)} of automatic decisions`,
      detail: `${n(k.overturned)} of ${n(k.auto_decided)}. The thresholds may be too lenient or too strict.`, ...(canConfigure ? { to: '/rules', cta: 'Review thresholds' } : {}) })
  }
  const blind = d.tests.filter((x) => x.total >= 20 && (x.unassessed_rate ?? 0) > 0.6)
  if (blind.length) {
    items.push({ sev: 'info', key: 'coverage', title: `${blind.map((t) => t.label).join(' and ')} could not assess most videos (${blind.map((t) => pct(t.unassessed_rate)).join(', ')})`,
      detail: 'Their results are rarely usable on this footage, so they add little to decisions. Check video quality or the test settings.' })
  }
  return items.sort((a, b) => SEV[a.sev] - SEV[b.sev]).slice(0, 5)
}

const SEV_ICON = { critical: '!', warning: '!', info: 'i' }

export function AttentionPanel({ items }) {
  if (!items.length) {
    return (
      <Card className="attention clear">
        <div className="att-clear"><span className="att-ico ok" aria-hidden="true">✓</span>
          <div><b>All clear</b><p className="sub">Nothing needs your attention right now: no videos over the review target, no failures, and every system is ready.</p></div></div>
      </Card>
    )
  }
  const worst = items[0].sev
  return (
    <Card title="Needs your attention" sub={`${items.length} item${items.length === 1 ? '' : 's'}, most urgent first`} className={`attention ${worst}`}>
      <ul className="att-list">
        {items.map((it) => (
          <li key={it.key} className={it.sev}>
            <span className={`att-ico ${it.sev}`} aria-hidden="true">{SEV_ICON[it.sev]}</span>
            <div className="att-text"><b>{it.title}</b><span>{it.detail}</span></div>
            {it.to && <Link className="button att-cta" to={it.to}>{it.cta} →</Link>}
          </li>
        ))}
      </ul>
    </Card>
  )
}

/* ---------- charts ---------- */
const niceMax = (v) => { const steps = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000]; return 4 * (steps.find((x) => 4 * x >= v) ?? Math.ceil(v / 4)) }
const DEC_COLOR = { Accept: 'var(--good)', 'Needs Review': 'var(--warn)', Reject: 'var(--bad)' }
const DECS = ['Accept', 'Needs Review', 'Reject']

export function VolumeChart({ data, bucket }) {
  const [tip, setTip] = useState(null)
  const W = 760, H = 230, L = 38, B = 26, T = 8
  const total = (d) => DECS.reduce((s, k) => s + (d[k] || 0), 0)
  const max = niceMax(Math.max(...data.map(total), 1))
  const slot = (W - L) / data.length
  const bw = Math.max(3, Math.min(26, slot - 3))
  const y = (v) => T + (H - T - B) * (1 - v / max)
  const every = Math.ceil(data.length / (bucket === 'hour' ? 8 : 9))
  const label = (t) => (bucket === 'hour' ? hourLabel(t) : dayLabel(t))
  if (!data.some((d) => total(d) > 0)) return <p className="empty-note">No analyzed videos in this period yet.</p>
  return (
    <div className="chart vol">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Videos analyzed per ${bucket}, by decision`}>
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <g key={f}><line x1={L} x2={W} y1={y(max * f)} y2={y(max * f)} className="grid" /><text x={L - 6} y={y(max * f) + 4} textAnchor="end" className="axis">{Math.round(max * f)}</text></g>
        ))}
        {data.map((d, i) => {
          const cx = L + slot * i + slot / 2
          let acc = 0
          return (
            <g key={d.t} onMouseMove={(e) => { const r = e.currentTarget.closest('.chart').getBoundingClientRect(); setTip({ x: e.clientX - r.left, y: e.clientY - r.top, d }) }} onMouseLeave={() => setTip(null)}>
              <rect x={cx - slot / 2} y={T} width={slot} height={H - T - B} fill="transparent" />
              {DECS.map((k) => {
                const v = d[k] || 0
                if (!v) return null
                const top = y(acc + v), h = y(acc) - top
                acc += v
                return <rect key={k} x={cx - bw / 2} y={top} width={bw} height={Math.max(h - 1.5, 1)} rx="2" fill={DEC_COLOR[k]} />
              })}
              {i % every === 0 && <text x={cx} y={H - 8} textAnchor="middle" className="axis">{label(d.t)}</text>}
            </g>
          )
        })}
      </svg>
      {tip && (
        <div className="tip tip-wide" style={{ left: tip.x, top: tip.y }}>
          <b>{label(tip.d.t)}</b> · {total(tip.d)} video{total(tip.d) === 1 ? '' : 's'}
          {DECS.map((k) => <div key={k}><i style={{ background: DEC_COLOR[k] }} />{k} {tip.d[k] || 0}</div>)}
        </div>
      )}
    </div>
  )
}

export function Donut({ segments, onPick, centerValue, centerLabel }) {
  const R = 52, C = 2 * Math.PI * R, total = segments.reduce((s, x) => s + x.count, 0)
  if (!total) return <p className="empty-note">No decisions yet.</p>
  let acc = 0
  return (
    <div className="donut-wrap">
      <svg viewBox="0 0 140 140" className="donut" role="img" aria-label="Decisions">
        <circle cx="70" cy="70" r={R} fill="none" stroke="var(--line)" strokeWidth="16" />
        {segments.map((s) => {
          if (!s.count) return null
          const len = (s.count / total) * C
          const el = (
            <circle key={s.label} cx="70" cy="70" r={R} fill="none" stroke={s.color} strokeWidth="16" strokeDasharray={`${Math.max(len - 2, 0.5)} ${C - Math.max(len - 2, 0.5)}`}
              strokeDashoffset={-acc} transform="rotate(-90 70 70)" style={{ cursor: 'pointer' }} onClick={() => onPick(s)}>
              <title>{`${s.label}: ${s.count.toLocaleString()} (${((s.count / total) * 100).toFixed(1)}%)`}</title>
            </circle>
          )
          acc += len
          return el
        })}
        <text x="70" y="68" textAnchor="middle" className="donut-n">{centerValue}</text>
        <text x="70" y="86" textAnchor="middle" className="donut-l">{centerLabel}</text>
      </svg>
      <ul className="donut-legend">
        {segments.map((s) => (
          <li key={s.label}>
            <button onClick={() => onPick(s)} disabled={!s.count}><i style={{ background: s.color }} />{s.display || s.label}<b>{n(s.count)}</b><span>{pct(s.count / total, s.count / total < 0.1 ? 1 : 0)}</span></button>
          </li>
        ))}
      </ul>
    </div>
  )
}

/* ---------- review pipeline ---------- */
export function Pipeline({ f }) {
  const rows = [
    ['Received', f.received, 'var(--brand)'], ['Analyzed', f.analyzed, 'var(--brand)'], ['Decided automatically', f.auto_decided, 'var(--good)'],
    ['Sent to a reviewer', f.sent_to_review, 'var(--warn)'], ['Decided by a reviewer', f.reviewed, 'var(--good)'],
  ]
  const max = Math.max(f.received, 1)
  return (
    <div className="pipeline">
      {rows.map(([label, v, color]) => (
        <div key={label} className="pl-row">
          <span className="pl-label">{label}</span>
          <span className="pl-track"><span style={{ width: `${(v / max) * 100}%`, background: color }} /></span>
          <b>{n(v)}</b><em>{f.received ? pct(v / f.received) : '—'}</em>
        </div>
      ))}
      <p className="sub pl-note">{n(f.waiting)} still waiting for a reviewer · reviewers overturned {n(f.overturned)} automatic decision{f.overturned === 1 ? '' : 's'}</p>
    </div>
  )
}

/* ---------- test scorecard ---------- */
function MixBar({ t, onPick }) {
  if (!t.total) return <span className="mix-empty">no results yet</span>
  return (
    <div className="mixbar" role="img" aria-label={`${t.label}: ${t.outcomes.filter((o) => o.count).map((o) => `${o.status} ${o.count}`).join(', ')}`}>
      {t.outcomes.filter((o) => o.count > 0).map((o) => (
        <button key={o.status} style={{ flex: o.count, background: toneColor(o.tone), opacity: o.tone === 'muted' ? 0.5 : 1 }}
          title={`${o.status}: ${o.count.toLocaleString()} (${((o.count / t.total) * 100).toFixed(1)}%) — click to list`} onClick={() => onPick(t, o)} />
      ))}
    </div>
  )
}

export function Scorecard({ tests, previous, onPick }) {
  const [open, setOpen] = useState(null)
  return (
    <div className="scorecard" role="table" aria-label="Results by test">
      <div className="sc-head" role="row">
        <span role="columnheader">Test</span>
        <span role="columnheader">Mix of results</span>
        <span role="columnheader" className="num"><i className="lg good" />Fine</span>
        <span role="columnheader" className="num"><i className="lg warn" />Needs a person</span>
        <span role="columnheader" className="num"><i className="lg muted" />Not assessed</span>
        <span />
      </div>
      {tests.map((t) => {
        const p = previous?.[t.key]
        const isOpen = open === t.key
        return (
          <div key={t.key} className={`sc-row ${isOpen ? 'open' : ''}`} role="row">
            <div className="sc-main">
              <div className="sc-name" role="cell"><b>{t.label}</b><span>{t.engine}</span></div>
              <div role="cell" className="sc-bar"><MixBar t={t} onPick={onPick} /></div>
              <div role="cell" className="num">{pct(t.clean_rate)}<small>{n(t.clean)}</small></div>
              <div role="cell" className="num attn">
                <span className="pctline">
                  {p && <Delta cur={t.attention_rate} prev={p.attention_rate} kind="pts" good="down" />}
                  <span className={t.attention_rate >= 0.15 ? 'hot' : ''}>{pct(t.attention_rate, t.attention_rate != null && t.attention_rate < 0.1 ? 1 : 0)}</span>
                </span>
                <small>{n(t.attention)}</small>
              </div>
              <div role="cell" className={`num ${t.unassessed_rate > 0.6 ? 'cold' : ''}`}>{pct(t.unassessed_rate)}<small>{n(t.unassessed)}</small></div>
              <button className="sc-toggle" onClick={() => setOpen(isOpen ? null : t.key)} aria-expanded={isOpen} aria-label={`${isOpen ? 'Hide' : 'Show'} details for ${t.label}`}>{isOpen ? '▾' : '▸'}</button>
            </div>
            {isOpen && (
              <div className="sc-detail">
                <div>
                  <p className="sub">{t.question}</p>
                  <table className="mini">
                    <tbody>
                      {t.outcomes.map((o) => (
                        <tr key={o.status} onClick={() => o.count && onPick(t, o)} className={o.count ? '' : 'zero'}>
                          <td><i className="dot" style={{ background: toneColor(o.tone), opacity: o.tone === 'muted' ? 0.5 : 1 }} />{o.status}</td>
                          <td className="num">{n(o.count)}</td><td className="num muted-text">{t.total ? pct(o.count / t.total, 1) : ''}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div>
                  <p className="sub">{t.distribution.label}</p>
                  {t.distribution.bins.some((c) => c > 0)
                    ? <Histogram bins={t.distribution.bins} label={t.distribution.label} lo={t.distribution.lo} hi={t.distribution.hi} unit={t.distribution.unit} markers={t.distribution.markers} />
                    : <p className="empty-note">No readings yet.</p>}
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

/* ---------- reasons and waiting list ---------- */
export function Reasons({ items, labels, onPick }) {
  if (!items.length) return <p className="empty-note">No exceptions in this period.</p>
  const max = Math.max(...items.map((i) => i.count))
  return (
    <ol className="reasons2">
      {items.map((r) => (
        <li key={r.parameter + r.reason}>
          <div className="rs-top"><span className="rs-text" title={r.reason}>{r.reason}</span><b>{n(r.count)}</b></div>
          <div className="rs-bar"><span style={{ width: `${(r.count / max) * 100}%` }} /></div>
          <span className="rs-tag">{labels[r.parameter] || r.parameter}</span>
        </li>
      ))}
    </ol>
  )
}

export function WaitChip({ since, sla }) {
  const h = hoursSince(since)
  const tone = h > sla ? 'bad' : h > sla * 0.5 ? 'warn' : 'good'
  return <span className={`badge ${tone}`} title={`Waiting since ${new Date(since).toLocaleString()} · target ${sla} h`}>{fmtHours(h)}</span>
}
