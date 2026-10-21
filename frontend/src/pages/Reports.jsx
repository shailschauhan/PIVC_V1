import { useEffect, useMemo, useState } from 'react'
import { api, query } from '../api.js'
import { Banner, tone } from '../components.jsx'
import { n, pct } from '../dash.jsx'
import { toneColor } from '../charts.jsx'

const PRESETS = [['7', 'Last 7 days'], ['30', 'Last 30 days'], ['90', 'Last 90 days'], ['all', 'All time'], ['custom', 'Custom']]
const day = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
const startOfDay = (d) => (d ? new Date(`${d}T00:00:00`).toISOString() : '')
const endOfDay = (d) => (d ? new Date(`${d}T23:59:59.999`).toISOString() : '')
const REVIEW_TONE = { NOT_REQUIRED: 'good', PENDING: 'warn', COMPLETED: 'info' }

function Chip({ on, onClick, children, kind }) {
  return <button type="button" className={`chip ${on ? 'on' : ''} ${kind ? `t-${kind}` : ''}`} aria-pressed={on} onClick={onClick}>{children}</button>
}

export default function Reports() {
  const [opts, setOpts] = useState(null)
  const [preset, setPreset] = useState('30')
  const [custom, setCustom] = useState({ from: '', to: '' })
  const [decisions, setDecisions] = useState([])
  const [reviews, setReviews] = useState([])
  const [tests, setTests] = useState({})            // test key -> chosen results
  const [cols, setCols] = useState(null)
  const [format, setFormat] = useState('csv')
  const [preview, setPreview] = useState(null)
  const [err, setErr] = useState('')
  const [showCols, setShowCols] = useState(false)
  const [aiOn, setAiOn] = useState(false)
  const [withAi, setWithAi] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => { api.aiStatus().then((s) => setAiOn(s.configured)).catch(() => {}) }, [])
  useEffect(() => { api.reportOptions().then((o) => { setOpts(o); setCols(new Set(o.default_columns)) }).catch((e) => setErr(e.message)) }, [])

  const range = useMemo(() => {
    if (preset === 'all') return { from: '', to: '' }
    if (preset === 'custom') return { from: startOfDay(custom.from), to: endOfDay(custom.to) }
    const from = new Date(); from.setDate(from.getDate() - Number(preset) + 1)
    return { from: startOfDay(day(from)), to: '' }
  }, [preset, custom])

  const filters = useMemo(() => ({
    date_from: range.from, date_to: range.to, decision: decisions, review_status: reviews,
    test: Object.entries(tests).flatMap(([k, sts]) => sts.map((s) => `${k}:${s}`)),
  }), [range, decisions, reviews, tests])

  // the preview follows the filters (after a short pause so ticking several boxes asks once)
  useEffect(() => {
    if (!opts) return undefined
    let live = true
    const t = setTimeout(() => {
      api.reportPreview(filters).then((p) => { if (live) { setPreview(p); setErr('') } }).catch((e) => live && setErr(e.message))
    }, 250)
    return () => { live = false; clearTimeout(t) }
  }, [filters, opts])

  const toggle = (list, setList, v) => setList(list.includes(v) ? list.filter((x) => x !== v) : [...list, v])
  const toggleTest = (k, status) => setTests((t) => {
    const cur = t[k] || []
    const next = cur.includes(status) ? cur.filter((x) => x !== status) : [...cur, status]
    const { [k]: _drop, ...rest } = t
    return next.length ? { ...rest, [k]: next } : rest
  })
  const toggleCol = (k) => setCols((c) => { const s = new Set(c); s.has(k) ? s.delete(k) : s.add(k); return s })

  if (!opts || !cols) return err ? <Banner kind="error">{err}</Banner> : <p>Loading…</p>

  const ordered = opts.column_groups.flatMap((g) => g.columns.map((c) => c.key)).filter((k) => cols.has(k))
  const pdfCap = opts.pdf.max_columns
  const total = preview?.total ?? 0
  const href = `/api/reports/export?${query({ ...filters, format, columns: ordered })}`
  // a PDF with an AI overview can fail (service down, key rejected), so it is fetched and any problem is shown here instead of in a broken download
  const downloadPdf = async () => {
    setBusy(true); setErr('')
    try {
      const res = await fetch(`/api/reports/export?${query({ ...filters, format: 'pdf', columns: ordered, ai: withAi ? 'true' : '' })}`, { credentials: 'same-origin' })
      if (!res.ok) { let m = res.statusText; try { m = (await res.json()).detail || m } catch { /* not JSON */ } throw new Error(m) }
      const url = URL.createObjectURL(await res.blob())
      const a = document.createElement('a'); a.href = url; a.download = (res.headers.get('content-disposition') || '').match(/filename=([^;]+)/)?.[1] || 'report.pdf'
      document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  const customBad = preset === 'custom' && custom.from && custom.to && custom.from > custom.to
  const canDownload = !!preview && total > 0 && ordered.length > 0 && !customBad
  const reset = () => { setPreset('30'); setDecisions([]); setReviews([]); setTests({}); setCols(new Set(opts.default_columns)) }

  return (
    <>
      <header className="page-head">
        <div><h2>Reports</h2><p>Choose the videos, then download them as a spreadsheet (CSV) or a printable summary (PDF). Every download is recorded in the audit log.</p></div>
      </header>
      {err && <Banner kind="error">{err}</Banner>}

      <div className="split">
        <div className="split-form report-form">
          <section className="card">
            <h3>1 · Uploaded between</h3>
            <div className="seg-wrap">{PRESETS.map(([k, l]) => <Chip key={k} on={preset === k} onClick={() => setPreset(k)}>{l}</Chip>)}</div>
            {preset === 'custom' && (
              <div className="date-row">
                <label className="date-pick">From<input type="date" value={custom.from} max={custom.to || day(new Date())} onChange={(e) => setCustom({ ...custom, from: e.target.value })} /></label>
                <label className="date-pick">To<input type="date" value={custom.to} min={custom.from || undefined} max={day(new Date())} onChange={(e) => setCustom({ ...custom, to: e.target.value })} /></label>
              </div>
            )}
            {customBad && <p className="field-error">The start date is after the end date.</p>}
          </section>

          <section className="card">
            <h3>2 · Decisions <span className="sub">leave all off to include every decision</span></h3>
            <div className="seg-wrap">
              {opts.decisions.map((d) => <Chip key={d} kind={tone(d)} on={decisions.includes(d)} onClick={() => toggle(decisions, setDecisions, d)}>{d}</Chip>)}
            </div>
            <h4>Review state</h4>
            <div className="seg-wrap">
              {opts.review_states.map((r) => <Chip key={r.key} kind={REVIEW_TONE[r.key]} on={reviews.includes(r.key)} onClick={() => toggle(reviews, setReviews, r.key)}>{r.label}</Chip>)}
            </div>
          </section>

          <section className="card">
            <h3>3 · Test results <span className="sub">a video must match every test you pick; within a test, any of the results</span></h3>
            <div className="test-pick">
              {opts.tests.map((t) => (
                <div key={t.key} className="test-row">
                  <b>{t.label}</b>
                  <div className="seg-wrap">{t.outcomes.map((o) => <Chip key={o} kind={tone(o)} on={(tests[t.key] || []).includes(o)} onClick={() => toggleTest(t.key, o)}>{o}</Chip>)}</div>
                </div>
              ))}
            </div>
          </section>

          <section className="card">
            <h3>4 · Columns
              <button type="button" className="link-btn" onClick={() => setShowCols(!showCols)} aria-expanded={showCols}>{showCols ? 'Hide' : `Choose (${ordered.length} selected)`}</button>
            </h3>
            {showCols && (
              <div className="col-groups">
                {opts.column_groups.map((g) => (
                  <fieldset key={g.group}><legend>{g.group}</legend>
                    {g.columns.map((c) => <label key={c.key} className="check"><input type="checkbox" checked={cols.has(c.key)} onChange={() => toggleCol(c.key)} /> {c.label}</label>)}
                  </fieldset>
                ))}
                <div className="col-actions">
                  <button type="button" onClick={() => setCols(new Set(opts.default_columns))}>All columns</button>
                  <button type="button" onClick={() => setCols(new Set(opts.pdf.default_columns))}>Short list (for a PDF)</button>
                  <button type="button" onClick={() => setCols(new Set())}>None</button>
                </div>
              </div>
            )}
            {!ordered.length && <p className="field-error">Pick at least one column.</p>}
          </section>
        </div>

        <aside className="split-side report-side">
          <section className="card sticky">
            <div className="rs-count">
              <b>{preview ? n(total) : '…'}</b>
              <span>video{total === 1 ? '' : 's'} match</span>
            </div>
            {preview && <p className="sub">{preview.filters.join(' · ')}</p>}
            {preview && total > 0 && (
              <>
                <div className="mix" role="img" aria-label="Decisions">
                  {Object.entries(preview.decisions).filter(([, c]) => c).map(([d, c]) => (
                    <span key={d} style={{ flex: c, background: toneColor(tone(d)) }} title={`${d}: ${n(c)}`} />
                  ))}
                </div>
                <ul className="mix-legend">
                  {Object.entries(preview.decisions).map(([d, c]) => (
                    <li key={d}><i style={{ background: toneColor(tone(d)) }} />{d}<b>{n(c)}</b><span>{pct(c / total)}</span></li>
                  ))}
                </ul>
                <details>
                  <summary>Results by test</summary>
                  {preview.tests.map((t) => (
                    <div key={t.key} className="mini-test"><b>{t.label}</b>
                      {t.results.filter((r) => r.count).map((r) => <span key={r.status} className={`badge ${r.tone === 'good' ? 'good' : r.tone === 'bad' ? 'bad' : r.tone === 'warn' ? 'warn' : 'muted'}`}>{r.status} {n(r.count)}</span>)}
                    </div>
                  ))}
                </details>
              </>
            )}
            {preview && total === 0 && <p className="empty-note">No videos match. Widen the dates or remove a filter.</p>}

            <h4>Format</h4>
            <div className="seg-wrap">
              <Chip on={format === 'csv'} onClick={() => setFormat('csv')}>CSV · spreadsheet</Chip>
              <Chip on={format === 'pdf'} onClick={() => setFormat('pdf')}>PDF · printable</Chip>
            </div>
            <p className="sub">
              {format === 'csv'
                ? 'Every matching video and every chosen column. Opens in Excel.'
                : `A summary of the counts, then up to ${n(opts.pdf.row_cap)} videos${total > opts.pdf.row_cap ? ` (you have ${n(total)}; use CSV for all)` : ''}, showing the first ${pdfCap} chosen columns${ordered.length > pdfCap ? ` — ${ordered.length - pdfCap} will be left out` : ''}.`}
            </p>
            {format === 'pdf' && aiOn && (
              <label className="check ai-opt"><input type="checkbox" checked={withAi} onChange={(e) => setWithAi(e.target.checked)} /> Add a written overview by AI
                <span className="sub">Only the counts above are sent, never individual videos or personal details.</span></label>
            )}
            {canDownload && format === 'pdf'
              ? <button className="primary block" onClick={downloadPdf} disabled={busy}>{busy ? 'Preparing…' : 'Download PDF'}</button>
              : canDownload
                ? <a className="button primary block" href={href} download>Download {format.toUpperCase()}</a>
                : <button className="primary block" disabled>Download {format.toUpperCase()}</button>}
            <button type="button" className="link-btn" onClick={reset}>Reset everything</button>
          </section>
        </aside>
      </div>
    </>
  )
}
