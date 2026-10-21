import { Fragment, useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { Banner, fmtDate } from '../components.jsx'
import { fmtMs, n, pct } from '../dash.jsx'

/* one word for how an engine is doing: Not ready beats Degraded beats Ready */
function health(e) {
  if (!e.ready) return { key: 'bad', text: 'Not ready' }
  const r = e.stats.error_rate
  if (e.stats.calls >= 5 && r >= 0.2) return { key: 'bad', text: 'Failing' }
  if (e.stats.calls >= 3 && r >= 0.05) return { key: 'warn', text: 'Degraded' }
  return { key: 'ok', text: 'Ready' }
}
const Pill = ({ e }) => { const h = health(e); return <span className={`pill ${h.key}`}><i aria-hidden="true" />{h.text}</span> }
const ago = (iso) => {
  if (!iso) return 'never'
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`
}

/* ---------------------------------------------------------------- charts */
function RunsChart({ series, hours }) {
  const W = 640, H = 150, L = 34, B = 22, T = 8, R = 6
  const max = Math.max(4, ...series.map((b) => b.calls))
  const nice = max <= 4 ? 4 : Math.ceil(max / 4) * 4
  const bw = (W - L - R) / series.length
  const y = (v) => T + (H - T - B) * (1 - v / nice)
  const label = (iso) => { const d = new Date(iso); return hours <= 48 ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }) : d.toLocaleDateString([], { day: 'numeric', month: 'short' }) }
  const every = Math.ceil(series.length / 6)
  return (
    <svg className="hchart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Runs per period, with failures in red">
      {[0, 0.5, 1].map((f) => <g key={f}><line x1={L} x2={W - R} y1={y(nice * f)} y2={y(nice * f)} className="grid" /><text x={L - 6} y={y(nice * f) + 4} textAnchor="end" className="axis">{Math.round(nice * f)}</text></g>)}
      {series.map((b, i) => {
        const ok = b.calls - b.errors
        return (
          <g key={b.at}>
            <title>{`${label(b.at)}: ${b.calls} run${b.calls === 1 ? '' : 's'}${b.errors ? `, ${b.errors} failed` : ''}${b.p50_ms != null ? `, median ${fmtMs(b.p50_ms)}` : ''}`}</title>
            <rect x={L + i * bw} y={T} width={bw} height={H - T - B} fill="transparent" />
            {ok > 0 && <rect x={L + i * bw + 1} width={Math.max(1, bw - 2)} y={y(ok + b.errors)} height={y(b.errors) - y(ok + b.errors)} rx="2" fill="var(--brand)" opacity="0.85" />}
            {b.errors > 0 && <rect x={L + i * bw + 1} width={Math.max(1, bw - 2)} y={y(b.errors)} height={y(0) - y(b.errors)} rx="2" fill="var(--bad)" />}
            {i % every === 0 && <text x={L + i * bw + bw / 2} y={H - 6} textAnchor="middle" className="axis">{label(b.at)}</text>}
          </g>
        )
      })}
    </svg>
  )
}

function LatencyChart({ series, hours }) {
  const pts = series.map((b, i) => ({ i, v: b.p50_ms, at: b.at })).filter((p) => p.v != null)
  const W = 640, H = 130, L = 46, B = 22, T = 8, R = 8
  if (!pts.length) return <p className="empty-note">No successful runs in this period yet.</p>
  const max = Math.max(...pts.map((p) => p.v)) * 1.15
  const x = (i) => L + ((W - L - R) * i) / Math.max(series.length - 1, 1)
  const y = (v) => T + (H - T - B) * (1 - v / max)
  const label = (iso) => { const d = new Date(iso); return hours <= 48 ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }) : d.toLocaleDateString([], { day: 'numeric', month: 'short' }) }
  const line = pts.map((p) => `${x(p.i)},${y(p.v)}`).join(' ')
  const every = Math.ceil(series.length / 6)
  return (
    <svg className="hchart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Median time per run">
      {[0, 0.5, 1].map((f) => <g key={f}><line x1={L} x2={W - R} y1={y(max * f)} y2={y(max * f)} className="grid" /><text x={L - 6} y={y(max * f) + 4} textAnchor="end" className="axis">{fmtMs(Math.round(max * f))}</text></g>)}
      <polyline points={line} fill="none" stroke="var(--brand)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
      {pts.map((p) => <circle key={p.i} cx={x(p.i)} cy={y(p.v)} r="3" fill="var(--surface)" stroke="var(--brand)" strokeWidth="2"><title>{`${label(p.at)}: median ${fmtMs(p.v)}`}</title></circle>)}
      {series.map((b, i) => i % every === 0 && <text key={b.at} x={x(i)} y={H - 6} textAnchor="middle" className="axis">{label(b.at)}</text>)}
    </svg>
  )
}

const Stat = ({ label, value, hint, tone }) => (
  <div className={`hstat ${tone || ''}`}><span>{label}</span><b>{value}</b>{hint && <small>{hint}</small>}</div>
)

/* ---------------------------------------------------------------- overview */
export default function Health() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [checking, setChecking] = useState(false)

  const load = useCallback(() => api.engines().then((d) => { setData(d); setErr('') }).catch((e) => setErr(e.message)), [])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) load() }, 30000)
    return () => clearInterval(t)
  }, [load])

  const checkNow = async () => {
    setChecking(true)
    try { setData(await api.checkEngines()); setErr('') } catch (e) { setErr(e.message) } finally { setChecking(false) }
  }
  if (!data) return err ? <Banner kind="error">{err}</Banner> : <p>Loading…</p>

  const states = data.engines.map(health)
  const bad = states.filter((s) => s.key === 'bad').length, warn = states.filter((s) => s.key === 'warn').length
  return (
    <>
      <header className="page-head">
        <div><h2>Service health</h2><p>Whether each test’s engine is ready, and how it has behaved on real videos in the last 24 hours.</p></div>
        <button onClick={checkNow} disabled={checking}>{checking ? 'Checking…' : 'Check now'}</button>
      </header>
      {err && <Banner kind="error">{err}</Banner>}
      <div className={`health-summary ${bad ? 'bad' : warn ? 'warn' : 'ok'}`} role="status">
        <b>{bad ? `${bad} of ${data.engines.length} not working` : warn ? `${warn} of ${data.engines.length} degraded` : 'All systems working'}</b>
        <span className="sub">Checked {ago(data.checked_at)}. Run counts, speed and failures come from the videos analysed, not from test pings.</span>
      </div>

      <div className="engine-grid">
        {data.engines.map((e) => (
          <Link key={e.key} to={`/health/${e.key}`} className="engine-card">
            <div className="ec-head">
              <div><h3>{e.label}</h3><span className="sub">{e.engine}</span></div>
              <Pill e={e} />
            </div>
            {!e.ready && <p className="ec-reason">{e.reason}</p>}
            <dl className="ec-stats">
              <div><dt>Median time</dt><dd>{fmtMs(e.stats.p50_ms)}</dd></div>
              <div><dt>Slowest 5%</dt><dd>{fmtMs(e.stats.p95_ms)}</dd></div>
              <div><dt>Failed runs</dt><dd className={e.stats.error_rate >= 0.05 ? 'hot' : ''}>{e.stats.calls ? pct(e.stats.error_rate, e.stats.error_rate && e.stats.error_rate < 0.1 ? 1 : 0) : '—'}</dd></div>
              <div><dt>Runs (24 h)</dt><dd>{n(e.stats.calls)}</dd></div>
            </dl>
            <div className="ec-foot">
              <span>{e.kind}{e.version ? ` · ${e.version}` : ''}</span>
              {e.probe_ms != null && <span title="How long the service took to answer the last health check">answers in {fmtMs(e.probe_ms)}</span>}
            </div>
          </Link>
        ))}
      </div>
    </>
  )
}

/* ---------------------------------------------------------------- one engine */
const RANGES = [[24, 'Last 24 hours'], [168, 'Last 7 days'], [720, 'Last 30 days']]

export function HealthDetail() {
  const { key } = useParams()
  const [hours, setHours] = useState(24)
  const [e, setE] = useState(null)
  const [err, setErr] = useState('')
  const load = useCallback(() => api.engine(key, hours).then((d) => { setE(d); setErr('') }).catch((x) => setErr(x.message)), [key, hours])
  useEffect(() => { setE(null); load() }, [load])

  if (!e) return err ? <><Link to="/health">← Service health</Link><Banner kind="error">{err}</Banner></> : <p>Loading…</p>
  const s = e.stats
  const rate = s.error_rate
  return (
    <>
      <p className="crumb"><Link to="/health">← Service health</Link></p>
      <header className="page-head">
        <div><h2>{e.label} <Pill e={e} /></h2><p>{e.question}</p></div>
        <select value={hours} onChange={(x) => setHours(Number(x.target.value))} aria-label="Period">
          {RANGES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </header>
      {!e.ready && <Banner kind="error"><b>Not ready:</b> {e.reason}</Banner>}

      <div className="hstats">
        <Stat label="Runs" value={n(s.calls)} hint={s.calls ? `${n(s.calls - s.errors)} succeeded` : 'none in this period'} />
        <Stat label="Failed runs" value={s.calls ? `${n(s.errors)} (${pct(rate, rate && rate < 0.1 ? 1 : 0)})` : '—'} tone={rate >= 0.2 ? 'bad' : rate >= 0.05 ? 'warn' : ''} hint="runs that ended in an error" />
        <Stat label="Median time" value={fmtMs(s.p50_ms)} hint="a typical successful run" />
        <Stat label="Slowest 5%" value={fmtMs(s.p95_ms)} hint={s.max_ms != null ? `slowest ${fmtMs(s.max_ms)}` : undefined} />
        <Stat label="Last success" value={ago(s.last_success)} hint={s.last_success ? fmtDate(s.last_success) : undefined} />
        {e.probe_ms != null && <Stat label="Health check" value={fmtMs(e.probe_ms)} hint="time to answer just now" />}
      </div>

      <div className="hcharts">
        <section className="card"><h3>Runs <span className="sub">blue succeeded · red failed</span></h3><RunsChart series={s.series} hours={hours} /></section>
        <section className="card"><h3>Median time per run</h3><LatencyChart series={s.series} hours={hours} /></section>
      </div>

      <div className="hcharts">
        <section className="card">
          <h3>About this engine</h3>
          <dl className="facts">
            <dt>Engine</dt><dd>{e.engine}</dd>
            <dt>Runs as</dt><dd>{e.kind}</dd>
            {e.version && <><dt>Version</dt><dd>{e.version}</dd></>}
            {e.models?.length > 0 && <><dt>Models</dt><dd>{e.models.join(', ')}</dd></>}
            {Object.entries(e.settings || {}).map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{String(v)}</dd></Fragment>)}
            <dt>Status now</dt><dd>{e.ready ? 'Ready' : e.reason}</dd>
          </dl>
        </section>
        <section className="card">
          <h3>Recent failures</h3>
          {s.recent_errors.length ? (
            <ul className="err-list">
              {s.recent_errors.map((x, i) => <li key={i}><span className="sub">{fmtDate(x.at)}</span><code>{x.message}</code></li>)}
            </ul>
          ) : <p className="empty-note">{s.calls ? 'No failures in this period.' : 'No runs in this period, so nothing to report.'}</p>}
          {s.last_error && !s.recent_errors.length && <p className="sub">Last failure: {ago(s.last_error.at)} — {s.last_error.message}</p>}
        </section>
      </div>
    </>
  )
}
