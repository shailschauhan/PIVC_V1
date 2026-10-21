import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { Badge, Banner, Confidence, LABELS, fmtDate } from '../components.jsx'
import { useAuth } from '../auth.jsx'

const busyRow = (c) => c.status === 'PROCESSING'

export default function Cases({ queue = false }) {
  const { can } = useAuth()
  const canAct = can('reviewer')
  const [sp] = useSearchParams()
  const drill = { parameter: sp.get('parameter') || '', parameter_status: sp.get('parameter_status') || '', days: sp.get('days') || '' }
  const [filters, setFilters] = useState({ q: '', decision: sp.get('decision') || '', status: sp.get('status') || '' })
  const [page, setPage] = useState(1)
  const [data, setData] = useState({ items: [], total: 0 })
  const [err, setErr] = useState('')
  const [selected, setSelected] = useState(() => new Set())
  const [working, setWorking] = useState(false)
  const [result, setResult] = useState(null)
  const [rerunTest, setRerunTest] = useState('')   // '' = all tests
  const pageSize = 20

  const load = useCallback(() => {
    const params = { ...filters, ...drill, page, page_size: pageSize, ...(queue ? { review_status: 'PENDING', sort: 'oldest' } : {}) }
    return api.cases(params).then(setData).catch((e) => setErr(e.message))
  }, [filters, page, queue, sp]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    load()
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [load])

  // A selection only makes sense for the list it was made in: never carry it over to a different filter.
  const firstRun = useRef(true)
  useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return }
    setSelected(new Set())
  }, [filters, sp, queue])

  const set = (k) => (e) => { setPage(1); setFilters({ ...filters, [k]: e.target.value }) }
  const pages = Math.max(1, Math.ceil(data.total / pageSize))

  const selectable = useMemo(() => data.items.filter((c) => !busyRow(c)), [data.items])
  const allOnPage = selectable.length > 0 && selectable.every((c) => selected.has(c.id))
  const someOnPage = selectable.some((c) => selected.has(c.id))
  const headRef = useRef(null)
  useEffect(() => { if (headRef.current) headRef.current.indeterminate = someOnPage && !allOnPage }, [someOnPage, allOnPage])

  const toggle = (id) => setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const togglePage = () => setSelected((s) => {
    const n = new Set(s)
    if (allOnPage) selectable.forEach((c) => n.delete(c.id)); else selectable.forEach((c) => n.add(c.id))
    return n
  })

  const reviewedCount = data.items.filter((c) => selected.has(c.id) && c.review_status === 'COMPLETED').length
  const run = async (action) => {
    const ids = [...selected]
    const n = ids.length
    const tests = action === 'rerun' && rerunTest ? [rerunTest] : undefined
    const what = tests ? LABELS[rerunTest] : 'all tests'
    const text = action === 'delete'
      ? `Permanently delete ${n} video${n > 1 ? 's' : ''}?\n\nThis removes the results, the review history and the stored files (including copies in blob storage). It cannot be undone.`
      : tests
        ? `Re-run ${what} on ${n} video${n > 1 ? 's' : ''}?\n\nThe other tests keep their results and each video's overall decision is recomputed. A completed review is reset only where the decision changes.`
        : `Re-run all tests on ${n} video${n > 1 ? 's' : ''}?\n\nThe current results are replaced${reviewedCount ? ` and ${reviewedCount} completed human review${reviewedCount > 1 ? 's' : ''} on this page will be reset (the previous decision is kept in the audit trail)` : ''}.`
    if (!window.confirm(text)) return
    setWorking(true); setResult(null); setErr('')
    try {
      const r = await api.bulk(action, ids, tests)
      const failed = r.results.filter((x) => !x.ok)
      setResult({ action, ...r, failed: failed })
      // keep only the ones that failed selected, so they can be retried or inspected
      setSelected(new Set(failed.map((x) => x.id)))
      await load()
    } catch (e) {
      setErr(e.message)
    } finally { setWorking(false) }
  }

  return (
    <>
      <header className="page-head">
        <div>
          <h2>{queue ? 'Review queue' : 'All videos'}</h2>
          <p>{queue ? 'Videos that need a human decision, longest-waiting first.' : `${data.total.toLocaleString()} video(s)`}</p>
        </div>
        <a className="button" href="/api/reports/export.csv">Export CSV</a>
      </header>
      {err && <Banner kind="error">{err}</Banner>}
      {result && (
        <Banner kind={result.failed.length ? 'warn' : 'info'}>
          {result.action === 'delete' ? 'Deleted' : `Re-run of ${rerunTest ? LABELS[rerunTest] : 'all tests'} started for`} <b>{result.succeeded}</b> video{result.succeeded === 1 ? '' : 's'}
          {result.failed.length > 0 && <>; <b>{result.failed.length}</b> could not be {result.action === 'delete' ? 'deleted' : 're-run'}:
            <ul className="plain">{result.failed.slice(0, 8).map((f) => <li key={f.id}>{f.case_ref || `#${f.id}`} — {f.message}</li>)}</ul></>}
        </Banner>
      )}
      {drill.parameter && (
        <Banner>Filtered: {LABELS[drill.parameter] || drill.parameter} = <b>{drill.parameter_status}</b>{drill.days && drill.days !== '0' ? ` · last ${drill.days} days` : ''}. <Link to="/cases">Clear</Link></Banner>
      )}
      <div className="filters">
        <input placeholder="Search file name, case ref, reference…" value={filters.q} onChange={set('q')} />
        {!queue && (
          <select value={filters.decision} onChange={set('decision')}>
            <option value="">All decisions</option>
            <option>Accept</option><option>Reject</option><option>Needs Review</option>
          </select>
        )}
        <select value={filters.status} onChange={set('status')}>
          <option value="">All statuses</option>
          <option value="PENDING">Pending</option><option value="PROCESSING">Processing</option>
          <option value="ANALYZED">Analyzed</option><option value="FAILED">Failed</option>
        </select>
      </div>

      {canAct && selected.size > 0 && (
        <div className="bulk-bar" role="region" aria-label="Actions for selected videos">
          <b>{selected.size} selected</b>
          <select className="rerun-select" value={rerunTest} onChange={(e) => setRerunTest(e.target.value)} disabled={working} aria-label="Which tests to re-run">
            <option value="">All tests</option>
            {Object.entries(LABELS).filter(([k]) => ['video_authenticity', 'health_indicators', 'age_estimation', 'face_match', 'liveness'].includes(k)).map(([k, l]) => <option key={k} value={k}>{l} only</option>)}
          </select>
          <button onClick={() => run('rerun')} disabled={working}>Re-run</button>
          {can('admin') && <button className="danger" onClick={() => run('delete')} disabled={working}>Delete</button>}
          <button onClick={() => setSelected(new Set())} disabled={working}>Clear selection</button>
          {working && <span className="sub">Working…</span>}
        </div>
      )}

      <section className="card flush">
        <table className="hover">
          <thead>
            <tr>
              {canAct && <th className="check-col">
                <input ref={headRef} type="checkbox" checked={allOnPage} onChange={togglePage} disabled={!selectable.length} aria-label="Select all videos on this page" />
              </th>}
              <th>Video</th><th>Reference</th><th>Authenticity</th><th>Fake score</th><th>Age</th><th>Vitals</th><th>Decision</th><th>Confidence</th><th>Review</th><th>Analyzed</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((c) => (
              <tr key={c.id} className={selected.has(c.id) ? 'selected' : ''}>
                {canAct && <td className="check-col">
                  <input type="checkbox" checked={selected.has(c.id)} onChange={() => toggle(c.id)} disabled={busyRow(c)}
                    aria-label={`Select ${c.filename}`} title={busyRow(c) ? 'Being analysed right now' : ''} />
                </td>}
                <td><Link to={`/cases/${c.id}`}>{c.filename}</Link><div className="sub">{c.case_ref}</div></td>
                <td>{c.reference || '—'}</td>
                <td>{c.status === 'ANALYZED' ? <Badge status={c.authenticity} /> : <span className="badge muted">{c.status}</span>}</td>
                <td>{c.fake_score != null ? c.fake_score.toFixed(3) : '—'}</td>
                <td>{c.age_status ? <><Badge status={c.age_status} />{c.estimated_age != null && c.age_status !== 'Unavailable' && <div className="sub">~{Math.round(c.estimated_age)} yrs{c.declared_age != null ? ` (declared ${c.declared_age})` : ''}</div>}</> : '—'}</td>
                <td>{c.vitals ? <><Badge status={c.vitals} />{c.heart_rate != null && c.vitals !== 'Unavailable' && <div className="sub">{Math.round(c.heart_rate)} bpm</div>}</> : '—'}</td>
                <td>{c.status === 'ANALYZED' && <Badge status={c.decision} />}</td>
                <td><Confidence value={c.confidence} /></td>
                <td>
                  {c.review_status === 'PENDING' && <span className="badge warn">Awaiting</span>}
                  {c.review_status === 'COMPLETED' && <Badge status={c.final_decision} />}
                  {c.review_status === 'NOT_REQUIRED' && <span className="muted-text">Auto</span>}
                </td>
                <td>{fmtDate(c.analyzed_at)}</td>
              </tr>
            ))}
            {!data.items.length && <tr><td colSpan={canAct ? 11 : 10} className="empty">No videos found.</td></tr>}
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
