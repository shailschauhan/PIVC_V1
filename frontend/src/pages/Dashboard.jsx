import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { Banner, Confidence } from '../components.jsx'
import { AttentionPanel, Card, Delta, Donut, Kpi, Pipeline, Reasons, Scorecard, Sparkline, SystemStrip, VolumeChart, WaitChip, attentionItems, fmtHours, fmtMs, n, pct, hoursSince } from '../dash.jsx'

const RANGES = [[1, '24 h'], [7, '7 days'], [30, '30 days'], [90, '90 days'], [0, 'All time']]
const REFRESH_MS = 15000
const RANGE_NAME = { 1: 'the last 24 hours', 7: 'the last 7 days', 30: 'the last 30 days', 90: 'the last 90 days', 0: 'all time' }
const readDays = () => { try { const raw = sessionStorage.getItem('dashDays'); const v = Number(raw); return raw !== null && RANGES.some(([x]) => x === v) ? v : 30 } catch { return 30 } }

function Skeleton() {
  return (
    <div aria-busy="true" aria-label="Loading dashboard">
      <div className="skel skel-strip" />
      <div className="kpi-grid">{[0, 1, 2, 3, 4].map((i) => <div key={i} className="skel skel-kpi" />)}</div>
      <div className="dash-row"><div className="skel skel-chart" /><div className="skel skel-chart" /></div>
    </div>
  )
}

export default function Dashboard() {
  const nav = useNavigate()
  const { can } = useAuth()
  const [days, setDays] = useState(readDays)
  const [d, setD] = useState(null)
  const [err, setErr] = useState('')

  const load = useCallback(() => api.dashboard(days).then((x) => { setD(x); setErr('') }).catch((e) => setErr(e.message)), [days])
  useEffect(() => {
    setD(null)                                  // a different period: show the skeleton, not the old numbers
    load()
    const t = setInterval(() => { if (document.visibilityState === 'visible') load() }, REFRESH_MS)
    return () => clearInterval(t)
  }, [load])
  useEffect(() => { try { sessionStorage.setItem('dashDays', String(days)) } catch { /* not remembered */ } }, [days])

  const go = (q) => nav(`/cases?${new URLSearchParams({ ...q, days })}`)
  const labels = useMemo(() => Object.fromEntries((d?.tests || []).map((t) => [t.key, t.label])), [d])
  const items = useMemo(() => (d ? attentionItems(d, can('admin')) : []), [d, can])
  const k = d?.kpis, pk = d?.previous?.kpis
  const totals = d?.trend.map((p) => p.Accept + p['Needs Review'] + p.Reject) || []
  const updated = d ? new Date(d.generated_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : null

  return (
    <>
      <header className="page-head dash-head">
        <div>
          <h2>Dashboard</h2>
          <p>{d ? <>Showing {RANGE_NAME[days]}{d.previous ? ', compared with the period before it' : ''} · updated {updated} · refreshes every {REFRESH_MS / 1000} s</> : 'Loading…'}</p>
        </div>
        <div className="actions">
          <div className="seg" role="group" aria-label="Period">
            {RANGES.map(([v, l]) => <button key={v} className={days === v ? 'on' : ''} aria-pressed={days === v} onClick={() => setDays(v)}>{l}</button>)}
          </div>
          {can('reviewer') && <Link className="button primary" to="/upload">Upload videos</Link>}
        </div>
      </header>
      {err && <Banner kind="error">Could not load the dashboard: {err}</Banner>}
      {!d && !err && <Skeleton />}
      {d && (
        <>
          <SystemStrip engines={d.engines} canConfigure={can('admin')} />

          {k.total === 0 && days === 0 ? (
            <Card className="onboard">
              <h3>No videos yet</h3>
              <p>Upload a PIVC video (with the customer's ID document photo if you have it) and every test runs automatically. Results, trends and anything that needs a person will appear here.</p>
              {can('reviewer') && <Link className="button primary" to="/upload">Upload your first video</Link>}
            </Card>
          ) : (
            <>
              <AttentionPanel items={items} />

              <div className="kpi-grid">
                <Kpi label="Videos analyzed" value={n(k.analyzed)} to={`/cases?days=${days}`} spark={<Sparkline values={totals} />}
                  delta={<Delta cur={k.analyzed} prev={pk?.analyzed} good="none" />} hint={`${n(k.total)} received${k.failed ? ` · ${n(k.failed)} failed` : ''}`} />
                <Kpi label="Decided automatically" value={pct(k.straight_through_rate)} delta={<Delta cur={k.straight_through_rate} prev={pk?.straight_through_rate} kind="pts" good="up" />}
                  hint={`${n(k.auto_decided)} without a person`} />
                <Kpi label="Waiting for a reviewer" value={n(d.backlog.pending)} to="/review" tone={d.backlog.over_sla ? 'critical' : ''}
                  hint={d.backlog.over_sla ? `${n(d.backlog.over_sla)} over the ${d.sla_hours} h target` : d.backlog.oldest[0] ? `oldest ${fmtHours(hoursSince(d.backlog.oldest[0].since))} · target ${d.sla_hours} h` : 'queue is clear'} />
                <Kpi label="Analysis time (median)" value={fmtMs(k.p50_ms)} delta={<Delta cur={k.p50_ms} prev={pk?.p50_ms} kind="pct" good="down" />}
                  hint={`95% finish within ${fmtMs(k.p95_ms)}`} />
                <Kpi label="Time to human decision" value={fmtHours(k.median_review_hours)} delta={<Delta cur={k.median_review_hours} prev={pk?.median_review_hours} kind="pct" good="down" />}
                  hint={k.queue_reviewed ? `median over ${n(k.queue_reviewed)} reviewed` : 'no reviews yet'} />
              </div>

              <div className="dash-row wide-left">
                <Card title="Volume by decision" sub={d.bucket === 'hour' ? 'Videos analyzed per hour' : 'Videos analyzed per day'}
                  action={<div className="legend inline">{[['Accept', 'var(--good)'], ['Sent to review', 'var(--warn)'], ['Reject', 'var(--bad)']].map(([l, c]) => <span key={l}><i style={{ background: c }} />{l}</span>)}</div>}>
                  <VolumeChart data={d.trend} bucket={d.bucket} />
                </Card>
                <Card title="Outcome" sub="Overall decision on analyzed videos">
                  <Donut centerValue={n(k.analyzed)} centerLabel="analyzed" onPick={(s) => go({ decision: s.label })}
                    segments={[{ label: 'Accept', count: k.accept, color: 'var(--good)' }, { label: 'Needs Review', display: 'Needs review', count: k.needs_review, color: 'var(--warn)' }, { label: 'Reject', count: k.reject, color: 'var(--bad)' }]} />
                  {k.avg_confidence != null && <p className="sub donut-foot">Average confidence <b>{pct(k.avg_confidence)}</b></p>}
                </Card>
              </div>

              <Card title="Test results" sub="How each of the five tests turned out. Click a bar segment or open a row for details.">
                <Scorecard tests={d.tests} previous={d.previous?.tests} onPick={(t, o) => go({ parameter: t.key, parameter_status: o.status })} />
                <p className="sc-note"><i className="lg good" />Fine = the result needs no action · <i className="lg warn" />Needs a person = flagged or inconclusive · <i className="lg muted" />Not assessed = the test could not judge (not checked, low quality or unavailable)</p>
              </Card>

              <div className="dash-row even">
                <div className="stack">
                  <Card title="Review pipeline" sub="What happened to the videos that came in">
                    <Pipeline f={d.funnel} />
                  </Card>
                  <Card flush title="Longest waiting for a reviewer" sub={d.backlog.over_sla ? `${n(d.backlog.over_sla)} over the ${d.sla_hours} h target` : `Target: decided within ${d.sla_hours} h`}
                    action={<Link to="/review" className="button">Open queue ({n(d.backlog.pending)}) →</Link>}>
                    <table className="hover">
                      <thead><tr><th>Video</th><th>Confidence</th><th className="num">Waiting</th></tr></thead>
                      <tbody>
                        {d.backlog.oldest.map((c) => (
                          <tr key={c.id}><td><Link to={`/cases/${c.id}`}>{c.filename}</Link></td><td><Confidence value={c.confidence} /></td>
                            <td className="num"><WaitChip since={c.since} sla={d.sla_hours} /></td></tr>
                        ))}
                        {!d.backlog.oldest.length && <tr><td colSpan="3" className="empty">Nothing is waiting for a reviewer.</td></tr>}
                      </tbody>
                    </table>
                  </Card>
                </div>
                <Card title="Top reasons for review" sub="Most common exception messages in this period">
                  <Reasons items={d.top_exceptions} labels={labels} />
                </Card>
              </div>
            </>
          )}
        </>
      )}
    </>
  )
}
