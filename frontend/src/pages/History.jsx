import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { Badge, Banner, fmtDate } from '../components.jsx'
import { fmtMs } from '../dash.jsx'

const TABS = [['', 'All'], ['active', 'In progress'], ['done', 'Completed'], ['failed', 'Failed']]
const TESTS = [['', 'Any test'], ['video_authenticity', 'Video authenticity'], ['health_indicators', 'Health indicators'],
  ['age_estimation', 'Age estimation'], ['face_match', 'Face match'], ['liveness', 'Liveness']]
const PERIODS = [[0, 'All time'], [1, 'Last 24 hours'], [7, 'Last 7 days'], [30, 'Last 30 days']]
const KIND = { first: 'First analysis', all: 'Re-run of everything', tests: 'Re-run of selected tests' }

function Elapsed({ since }) {
  const [, tick] = useState(0)
  useEffect(() => { const t = setInterval(() => tick((n) => n + 1), 1000); return () => clearInterval(t) }, [])
  return <>{fmtMs(Math.max(0, Date.now() - new Date(since).getTime()))}</>
}

function Status({ r }) {
  if (r.status === 'RUNNING') return <span className="run-status running"><i aria-hidden="true" />Running</span>
  if (r.status === 'QUEUED') return <span className="run-status queued"><i aria-hidden="true" />Waiting{r.ahead ? <small>{r.ahead} ahead</small> : <small>next</small>}</span>
  if (r.status === 'DONE') return <span className="run-status done"><i aria-hidden="true" />Done</span>
  if (r.status === 'CANCELLED') return <span className="run-status failed"><i aria-hidden="true" />Cancelled</span>
  return <span className="run-status failed"><i aria-hidden="true" />Failed</span>
}

function Result({ r }) {
  if (r.status === 'RUNNING' || r.status === 'QUEUED') return <span className="muted-text">{r.status === 'RUNNING' ? 'In progress…' : 'Not started yet'}</span>
  if (r.status !== 'DONE') return <span className="run-error">{r.error || 'Could not be completed.'}</span>
  const decisionChanged = r.decision_before && r.decision_before !== r.decision_after
  return (
    <div className="run-results">
      {r.results.map((x) => (
        <div key={x.key} className="rr-line">
          <span className="rr-test">{x.test}</span>
          {x.from && x.from !== x.to ? <><Badge status={x.from} /><i className="arrow" aria-hidden="true">→</i></> : null}
          <Badge status={x.to} />
          {x.from && x.from === x.to && <span className="sub">unchanged</span>}
        </div>
      ))}
      {r.decision_after && (
        <div className="rr-decision">
          Overall decision: {decisionChanged ? <><Badge status={r.decision_before} /><i className="arrow" aria-hidden="true">→</i></> : null}<Badge status={r.decision_after} />
          {r.decision_before && !decisionChanged && <span className="sub"> unchanged</span>}
        </div>
      )}
    </div>
  )
}

export default function History() {
  const [tab, setTab] = useState('')
  const [test, setTest] = useState('')
  const [days, setDays] = useState(0)
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const seq = useRef(0)

  const load = useCallback(() => {
    const mine = ++seq.current
    return api.history({ status: tab, test, days: days || '', page, page_size: 25 })
      .then((d) => { if (mine === seq.current) { setData(d); setErr('') } })
      .catch((e) => mine === seq.current && setErr(e.message))
  }, [tab, test, days, page])
  useEffect(() => { load() }, [load])
  const active = data ? data.active.queued + data.active.running : 0
  useEffect(() => {  // quicker while something is in progress
    const t = setInterval(() => { if (!document.hidden) load() }, active ? 3000 : 15000)
    return () => clearInterval(t)
  }, [load, active])

  const pages = data ? Math.max(1, Math.ceil(data.total / 25)) : 1

  return (
    <>
      <header className="page-head">
        <div>
          <h2>History</h2>
          <p>Every test run: what is being processed right now, and what ran before. Re-running a single test appears here too.</p>
        </div>
        {data && (
          <div className="live-pill" role="status">
            {active ? <><i aria-hidden="true" />{data.active.running} running · {data.active.queued} waiting</> : <>Nothing is being processed</>}
          </div>
        )}
      </header>
      {err && <Banner kind="error">{err}</Banner>}

      <div className="filters history-filters">
        <div className="seg" role="tablist" aria-label="Show">
          {TABS.map(([k, l]) => <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => { setTab(k); setPage(1) }}>
            {l}{k === 'active' && active > 0 ? <span className="count-dot">{active}</span> : null}</button>)}
        </div>
        <select value={test} onChange={(e) => { setTest(e.target.value); setPage(1) }} aria-label="Test">{TESTS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select>
        <select value={days} onChange={(e) => { setDays(Number(e.target.value)); setPage(1) }} aria-label="Period">{PERIODS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select>
      </div>

      <section className="card flush">
        <table className="history-table">
          <thead><tr><th>Status</th><th>Video</th><th>Tests</th><th>Requested</th><th>Time taken</th><th>Result</th></tr></thead>
          <tbody>
            {data?.items.map((r) => (
              <tr key={r.id} className={`run-row ${r.status.toLowerCase()}`}>
                <td><Status r={r} /></td>
                <td>{r.case_id ? <Link to={`/cases/${r.case_id}`}>{r.filename}</Link> : <span title="This video has been deleted">{r.filename}</span>}<div className="sub">{r.case_ref}{!r.case_id && ' · deleted'}</div></td>
                <td><div className="test-chips">{r.tests.map((t) => <span key={t} className={`test-chip ${t === 'All tests' ? 'all' : ''}`}>{t}</span>)}</div><div className="sub">{KIND[r.kind]}</div></td>
                <td title={fmtDate(r.requested_at)}>{fmtDate(r.requested_at)}<div className="sub">by {r.requested_by === 'system' ? 'the system' : r.requested_by}</div></td>
                <td className="nowrap">{r.status === 'RUNNING' && r.started_at ? <Elapsed since={r.started_at} /> : r.duration_ms != null ? fmtMs(r.duration_ms) : '—'}</td>
                <td><Result r={r} /></td>
              </tr>
            ))}
            {data && !data.items.length && <tr><td colSpan="6" className="empty">{tab === 'active' ? 'Nothing is being processed right now.' : 'No runs match these filters.'}</td></tr>}
            {!data && !err && <tr><td colSpan="6" className="empty">Loading…</td></tr>}
          </tbody>
        </table>
      </section>
      {pages > 1 && (
        <div className="pager">
          <button disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
          <span>Page {page} of {pages}</span>
          <button disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      )}
    </>
  )
}
