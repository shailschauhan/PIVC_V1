import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, query } from '../api.js'
import { Banner, fmtDate } from '../components.jsx'

const PAGE = 50
const TONE = { video: 'info', review: 'good', rules: 'warn', settings: 'warn', users: 'info', security: 'muted', reports: 'muted', system: 'muted' }
const startOfDay = (d) => (d ? new Date(`${d}T00:00:00`).toISOString() : '')
const endOfDay = (d) => (d ? new Date(`${d}T23:59:59.999`).toISOString() : '')
const EMPTY = { q: '', category: '', action: '', actor: '', from: '', to: '' }

function when(iso) {
  const d = new Date(iso)
  const mins = Math.round((Date.now() - d.getTime()) / 60000)
  const rel = mins < 1 ? 'just now' : mins < 60 ? `${mins} min ago` : mins < 1440 ? `${Math.round(mins / 60)} h ago` : mins < 10080 ? `${Math.round(mins / 1440)} d ago` : null
  return { rel, abs: d.toLocaleString() }
}

export default function Audit() {
  const [f, setF] = useState(EMPTY)
  const [applied, setApplied] = useState(EMPTY)
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [facets, setFacets] = useState({ categories: [], actions: [], actors: [] })
  const [open, setOpen] = useState(null)
  const [err, setErr] = useState('')
  const seq = useRef(0)

  useEffect(() => { api.auditFacets().then(setFacets).catch(() => {}) }, [])
  // typing in the search box waits a moment before asking; the other filters apply at once
  useEffect(() => {
    const t = setTimeout(() => { setApplied(f); setPage(1) }, f.q !== applied.q ? 350 : 0)
    return () => clearTimeout(t)
  }, [f]) // eslint-disable-line react-hooks/exhaustive-deps

  const params = useCallback((a) => ({ q: a.q.trim(), category: a.category, action: a.action, actor: a.actor, date_from: startOfDay(a.from), date_to: endOfDay(a.to) }), [])
  const load = useCallback(() => {
    const mine = ++seq.current
    api.audit({ ...params(applied), page, page_size: PAGE })
      .then((d) => { if (mine === seq.current) { setData(d); setErr('') } })
      .catch((e) => mine === seq.current && setErr(e.message))
  }, [applied, page, params])
  useEffect(() => { load() }, [load])

  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })
  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE)) : 1
  const filtered = JSON.stringify(f) !== JSON.stringify(EMPTY)

  return (
    <>
      <header className="page-head">
        <div><h2>Audit log</h2><p>Who uploaded, decided, re-ran, deleted or changed a setting, and when. Entries cannot be edited or removed here.</p></div>
        <a className="button" href={`/api/audit/export.csv?${query(params(applied))}`}>Export CSV{data ? ` (${data.total.toLocaleString()})` : ''}</a>
      </header>
      {err && <Banner kind="error">{err}</Banner>}

      <div className="filters audit-filters">
        <input type="search" placeholder="Search who, what, video or detail…" value={f.q} onChange={set('q')} aria-label="Search the audit log" />
        <select value={f.category} onChange={set('category')} aria-label="Area">
          <option value="">All areas</option>
          {facets.categories.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
        </select>
        <select value={f.action} onChange={set('action')} aria-label="Action">
          <option value="">All actions</option>
          {facets.actions.map((a) => <option key={a}>{a}</option>)}
        </select>
        <select value={f.actor} onChange={set('actor')} aria-label="Who">
          <option value="">Everyone</option>
          {facets.actors.map((a) => <option key={a}>{a}</option>)}
        </select>
        <label className="date-pick">From<input type="date" value={f.from} max={f.to || undefined} onChange={set('from')} /></label>
        <label className="date-pick">To<input type="date" value={f.to} min={f.from || undefined} onChange={set('to')} /></label>
        {filtered && <button onClick={() => setF(EMPTY)}>Clear</button>}
      </div>

      <section className="card flush">
        <table className="audit-table">
          <thead><tr><th>When</th><th>Who</th><th>Area</th><th>What happened</th><th aria-label="Details" /></tr></thead>
          <tbody>
            {data?.items.map((a) => {
              const w = when(a.at)
              const isOpen = open === a.id
              return [
                <tr key={a.id} className={`audit-row ${isOpen ? 'open' : ''}`} onClick={() => setOpen(isOpen ? null : a.id)}>
                  <td className="nowrap" title={w.abs}>{w.rel || w.abs}{w.rel && <div className="sub">{w.abs}</div>}</td>
                  <td>{a.actor === 'system' ? <span className="muted-text">system</span> : a.actor}</td>
                  <td><span className={`badge ${a.action === 'sign-in failed' || a.action === 'account locked' ? 'bad' : TONE[a.category] || 'muted'}`}>{facets.categories.find((c) => c.key === a.category)?.label || a.category}</span></td>
                  <td>{a.summary}{a.case_id && a.action !== 'deleted' && <> · <Link to={`/cases/${a.case_id}`} onClick={(e) => e.stopPropagation()}>open video</Link></>}</td>
                  <td className="chev"><button className="icon-btn" aria-expanded={isOpen} aria-label={isOpen ? 'Hide details' : 'Show details'}>{isOpen ? '▾' : '▸'}</button></td>
                </tr>,
                isOpen && (
                  <tr key={`${a.id}-d`} className="audit-detail"><td colSpan="5">
                    <dl>
                      <dt>Action</dt><dd>{a.action}</dd>
                      {a.target && <><dt>Concerning</dt><dd>{a.target}</dd></>}
                      <dt>Time</dt><dd>{fmtDate(a.at)}</dd>
                      {a.ip && <><dt>IP address</dt><dd>{a.ip}</dd></>}
                      <dt>Details</dt><dd><pre>{JSON.stringify(a.detail, null, 2)}</pre></dd>
                    </dl>
                  </td></tr>
                ),
              ]
            })}
            {data && !data.items.length && <tr><td colSpan="5" className="empty">{filtered ? 'Nothing matches these filters.' : 'Nothing has been recorded yet.'}</td></tr>}
            {!data && !err && <tr><td colSpan="5" className="empty">Loading…</td></tr>}
          </tbody>
        </table>
      </section>
      {pages > 1 && (
        <div className="pager">
          <button disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
          <span>Page {page.toLocaleString()} of {pages.toLocaleString()}</span>
          <button disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      )}
    </>
  )
}
