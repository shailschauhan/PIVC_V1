import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { Badge, Banner, fmtDate, tone } from '../components.jsx'
import { n, pct } from '../dash.jsx'

// [label, help, step] for numbers; ['bool', help] for switches. Keys are the rule names the server knows.
const GROUPS = [
  ['Video authenticity', 'Decides Accept, Reject or Needs Review. Scores are not calibrated probabilities: set these from checks on labelled videos.', {
    fake_score_review: ['Review from fake score', 'Ensemble fake score at or above this is no longer a clean Pass.', 0.01],
    fake_score_flag: ['Suspected manipulation from fake score', 'At or above this the video is flagged (DeepFakeSol default 0.30).', 0.01],
    confidence_threshold: ['Confidence threshold', 'Verdicts with lower confidence are never decided automatically; they go to a reviewer.', 0.01],
    min_face_frames: ['Minimum frames with a face', 'Fewer detected-face frames than this → Needs Review.', 1],
    bg_blur_ratio_max: ['Blurred-background cutoff', 'Background/face sharpness ratio below this is treated as a blurred (virtual) background.', 0.01],
    bg_static_diff_max: ['Static-background cutoff', 'Frame-to-frame background change below this is treated as a pasted or static background.', 0.01],
    virtual_bg_blocks_auto_reject: ['bool', 'Never reject automatically when the video also has a virtual background; a person confirms first.'],
  }],
  ['Face match (ID document)', 'Compares the person in the video with the photo on the ID document.', {
    face_match_threshold: ['Match at similarity ≥', 'Median similarity to the ID photo at or above this, with most frames matching, is a Match.', 0.01],
    face_mismatch_threshold: ['Mismatch below similarity', 'Below this on good-quality images is a Mismatch; in between is Review. Poor-quality images can never be a Mismatch.', 0.01],
    face_mismatch_requires_review: ['bool', 'Send a video whose face does not match (or is inconclusive) to a reviewer.'],
  }],
  ['Liveness (risk signal)', 'A passive check that the face is a real, present person. A risk signal, not proof.', {
    liveness_pass_threshold: ['Pass at real-face probability ≥', 'Median probability that the face is real, at or above this, is a Pass.', 0.01],
    liveness_spoof_threshold: ['Suspected attack below', 'Consistently below this on judgeable frames means a possible presentation attack.', 0.01],
    liveness_suspected_requires_review: ['bool', 'Send a video with a suspected presentation attack to a reviewer.'],
  }],
  ['Age estimation', 'Compares the estimated age with the age or date of birth supplied with the upload.', {
    age_tolerance_years: ['Tolerance (years)', 'Estimated age further than this from the declared age is a mismatch (Review).', 1],
    age_min_frames: ['Minimum frames with a face', 'Fewer usable frames → Low confidence (never a mismatch).', 1],
    age_max_spread: ['Maximum spread (years)', 'Per-frame estimates that vary more than this → Low confidence.', 1],
    age_mismatch_requires_review: ['bool', 'Send a video whose estimated age does not match the declared age to a reviewer.'],
  }],
  ['Health indicators (vital signs)', 'Informational unless you switch the last option on.', {
    health_sqi_min: ['Minimum signal quality', 'Readings with a lower signal-quality index are shown but never trusted or flagged.', 0.05],
    health_min_seconds: ['Minimum video length (s)', 'Shorter videos cannot give a reliable reading.', 1],
    hr_normal_low: ['Heart rate — low (bpm)', 'Below this a reliable reading is flagged.', 1],
    hr_normal_high: ['Heart rate — high (bpm)', 'Above this a reliable reading is flagged.', 1],
    resp_normal_low: ['Breathing — low (per min)', 'Below this a reliable reading is flagged.', 1],
    resp_normal_high: ['Breathing — high (per min)', 'Above this a reliable reading is flagged.', 1],
    health_flag_requires_review: ['bool', 'Send a video with a flagged vital sign to a reviewer (instead of only reporting it).'],
  }],
  ['Operations', 'Does not change any decision.', {
    review_sla_hours: ['Review target (hours)', 'A video waiting longer than this for a human decision counts as over target on the dashboard.', 1],
  }],
]
const SPEC = Object.fromEntries(GROUPS.flatMap(([, , f]) => Object.entries(f)))
const label = (k) => (SPEC[k][0] === 'bool' ? SPEC[k][1] : SPEC[k][0])
const show = (v) => (v === true ? 'on' : v === false ? 'off' : v)
const PERIODS = [[7, 'Last 7 days'], [30, 'Last 30 days'], [90, 'Last 90 days'], [0, 'All time']]
const DECISIONS = ['Accept', 'Needs Review', 'Reject']

/* ---------------------------------------------------------------- what would change */
function Delta({ a, b }) {
  const d = b - a
  if (!d) return <span className="muted-text">no change</span>
  return <span className={d > 0 ? 'up' : 'down'}>{d > 0 ? '+' : '−'}{n(Math.abs(d))}</span>
}

function Impact({ r, days, setDays, loading }) {
  const rv = r.reviewers
  const share = (v) => pct(rv.reviewed ? v / rv.reviewed : 0)
  const worse = rv.reviewed && rv.proposed.disagree > rv.current.disagree
  return (
    <section className={`card impact ${loading ? 'busy' : ''}`} aria-live="polite" aria-busy={loading}>
      <div className="impact-head">
        <h3>What would change</h3>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Videos to compare">
          {PERIODS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </div>
      <p className="sub">
        Re-judged {n(r.examined)} past video{r.examined === 1 ? '' : 's'} with the stored measurements. Nothing is re-run and nothing is saved.
        {r.sampled && ` Only the newest ${n(r.examined + r.skipped)} of ${n(r.in_window)} were examined.`}
        {r.skipped > 0 && ` ${n(r.skipped)} could not be re-judged (older format).`}
      </p>

      {!r.examined ? <p className="empty-note">There are no analysed videos in this period to compare.</p> : (
        <>
          <div className="dec-tiles">
            {DECISIONS.map((d) => (
              <div key={d} className="dec-tile">
                <span className={`badge ${tone(d)}`}>{d}</span>
                <b>{n(r.decisions.current[d])} <i aria-hidden="true">→</i> {n(r.decisions.proposed[d])}</b>
                <Delta a={r.decisions.current[d]} b={r.decisions.proposed[d]} />
              </div>
            ))}
          </div>

          {r.flips.total === 0
            ? <p className="no-flip">No past video would get a different decision.</p>
            : (
              <>
                <p className="flip-line"><b>{n(r.flips.total)}</b> video{r.flips.total === 1 ? '' : 's'} ({pct(r.flips.total / r.examined, r.flips.total / r.examined < 0.1 ? 1 : 0)}) would get a different decision:</p>
                <ul className="flips">
                  {r.flips.matrix.map((m) => <li key={m.from + m.to}><b>{n(m.count)}</b><Badge status={m.from} /><i aria-hidden="true">→</i><Badge status={m.to} /></li>)}
                </ul>
              </>
            )}

          {rv.reviewed > 0 && (
            <>
              <h4>Against what reviewers decided <span className="sub">({n(rv.reviewed)} videos a reviewer decided)</span></h4>
              <table className="mini">
                <thead><tr><th /><th>Today</th><th>After the change</th></tr></thead>
                <tbody>
                  <tr><td>Automatic decision matches the reviewer</td><td>{share(rv.current.agree)}</td><td>{share(rv.proposed.agree)}</td></tr>
                  <tr className={worse ? 'bad-row' : ''}><td>Automatic decision contradicts the reviewer</td><td>{share(rv.current.disagree)}</td><td>{share(rv.proposed.disagree)}</td></tr>
                  <tr><td>Still sent to a person</td><td>{share(rv.current.to_person)}</td><td>{share(rv.proposed.to_person)}</td></tr>
                </tbody>
              </table>
              {worse && <Banner kind="warn">{n(rv.proposed.disagree - rv.current.disagree)} more video{rv.proposed.disagree - rv.current.disagree === 1 ? '' : 's'} would be decided automatically the opposite way to what a reviewer chose. Check the examples below before saving.</Banner>}
            </>
          )}

          {r.tests.length > 0 && (
            <>
              <h4>Results that would change</h4>
              <ul className="test-changes">
                {r.tests.map((t) => (
                  <li key={t.key}><b>{t.label}</b> · {n(t.changed)} video{t.changed === 1 ? '' : 's'}
                    <span className="sub"> {Object.keys({ ...t.current, ...t.proposed }).filter((s) => (t.current[s] || 0) !== (t.proposed[s] || 0))
                      .map((s) => `${s} ${n(t.current[s] || 0)} → ${n(t.proposed[s] || 0)}`).join(' · ')}</span></li>
                ))}
              </ul>
            </>
          )}

          {r.examples.length > 0 && (
            <>
              <h4>Examples</h4>
              <table className="mini examples">
                <tbody>
                  {r.examples.map((e) => (
                    <tr key={e.id}>
                      <td><Link to={`/cases/${e.id}`}>{e.filename}</Link></td>
                      <td><Badge status={e.from} /> <i aria-hidden="true">→</i> <Badge status={e.to} /></td>
                      <td className="sub">{e.final_decision ? `Reviewer: ${e.final_decision}` : 'Not reviewed'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {r.flips.total > r.examples.length && <p className="sub">Showing {r.examples.length} of {n(r.flips.total)}.</p>}
            </>
          )}

          {r.replay_matches_stored != null && (
            <p className={`sub replay ${r.replay_matches_stored < 0.95 ? 'low' : ''}`}>
              Method check: re-judging these videos with today’s rules reproduces the decision they actually got for {pct(r.replay_matches_stored, 1)} of them
              {r.replay_matches_stored < 0.95 ? ' — lower than expected, because the rules changed since they were analysed or some were analysed by an older version. Treat the numbers as approximate.' : '.'}
            </p>
          )}
        </>
      )}
    </section>
  )
}

/* ---------------------------------------------------------------- history */
function History({ onLoad, onRestore, busy, current }) {
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => { api.rulesHistory().then((d) => setRows(d.versions)).catch((e) => setErr(e.message)) }, [current])
  if (err) return <Banner kind="error">{err}</Banner>
  if (!rows) return <p>Loading…</p>
  return (
    <ol className="versions">
      {rows.map((v) => (
        <li key={v.version} className={v.current ? 'current' : ''}>
          <div className="v-head">
            <b>Version {v.version}</b>{v.current && <span className="badge good">In use</span>}
            <span className="sub">{fmtDate(v.at)} · {v.by || 'unknown'}</span>
          </div>
          {v.note && <p className="v-note">“{v.note}”</p>}
          <ul className="v-changes">
            {v.changes.length ? v.changes.map((c) => (
              <li key={c.key}>{label(c.key)}: <s>{show(c.from)}</s> → <b>{show(c.to)}</b></li>
            )) : <li className="muted-text">No differences from the previous version.</li>}
          </ul>
          {!v.current && (
            <div className="v-actions">
              <button onClick={() => onLoad(v.version, rows)} disabled={busy}>Load into the editor</button>
              <button onClick={() => onRestore(v.version)} disabled={busy}>Restore now</button>
            </div>
          )}
        </li>
      ))}
      {!rows.length && <li className="muted-text">No rule has been changed yet: the built-in defaults are in use.</li>}
      {rows.length > 0 && <li className="muted-text">Before version 1, the built-in defaults were in use.</li>}
    </ol>
  )
}

/* ---------------------------------------------------------------- page */
export default function Rules() {
  const [server, setServer] = useState(null)
  const [draft, setDraft] = useState({})
  const [tab, setTab] = useState('edit')
  const [days, setDays] = useState(30)
  const [impact, setImpact] = useState(null)
  const [impactErr, setImpactErr] = useState('')
  const [loading, setLoading] = useState(false)
  const [note, setNote] = useState('')
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)
  const [kpis, setKpis] = useState(null)
  const noteRef = useRef(null)

  useEffect(() => { api.rules().then(setServer).catch((e) => setMsg({ kind: 'error', text: e.message })) }, [])
  useEffect(() => { api.dashboard(30).then((d) => setKpis(d.kpis)).catch(() => {}) }, [server?.version])

  const values = useMemo(() => (server ? { ...server.rules, ...draft } : null), [server, draft])
  const keys = Object.keys(draft)
  const blank = keys.some((k) => draft[k] === '' || Number.isNaN(draft[k]))

  // the preview follows the edits, after a short pause
  useEffect(() => {
    if (!keys.length || blank) { setImpact(null); setImpactErr(''); return undefined }
    let live = true
    setLoading(true)
    const t = setTimeout(() => {
      api.previewRules(draft, days)
        .then((r) => { if (live) { setImpact(r); setImpactErr('') } })
        .catch((e) => { if (live) { setImpact(null); setImpactErr(e.message) } })
        .finally(() => live && setLoading(false))
    }, 450)
    return () => { live = false; clearTimeout(t) }
  }, [draft, days]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!server) return msg ? <Banner kind="error">{msg.text}</Banner> : <p>Loading…</p>

  const setValue = (k, v) => setDraft((d) => {
    const { [k]: _old, ...rest } = d
    return v === server.rules[k] ? rest : { ...rest, [k]: v }
  })
  const onNum = (k) => (e) => { const t = e.target.value; setDraft((d) => ({ ...d, [k]: t === '' ? '' : Number(t) })) }
  const onBlurNum = (k) => () => { if (draft[k] === server.rules[k]) setValue(k, server.rules[k]) }

  const save = async () => {
    setBusy(true); setMsg(null)
    try {
      const r = await api.saveRules(draft, note)
      setServer(r); setDraft({}); setNote(''); setImpact(null)
      setMsg({ kind: 'info', text: `Saved as version ${r.version}. New analyses use these rules; videos already analysed keep their results until re-run.` })
    } catch (e) { setMsg({ kind: 'error', text: e.message }) } finally { setBusy(false) }
  }
  const restore = async (v) => {
    if (!window.confirm(`Restore the rules of version ${v}?\n\nThis becomes a new version. Videos already analysed keep their results.`)) return
    setBusy(true); setMsg(null)
    try {
      const r = await api.restoreRules(v)
      setServer(r); setDraft({})
      setMsg({ kind: 'info', text: `Restored version ${v} as version ${r.version}.` })
    } catch (e) { setMsg({ kind: 'error', text: e.message }) } finally { setBusy(false) }
  }
  const loadVersion = (v, rows) => {
    const target = rows.find((x) => x.version === v)
    // rebuild that version's full rules from the history: walk back from the newest, undoing changes
    let rules = { ...server.rules }
    for (const row of rows) { if (row.version === v) break; for (const c of row.changes) rules[c.key] = c.from }
    const next = Object.fromEntries(Object.entries(rules).filter(([k, val]) => val !== server.rules[k]))
    setDraft(next); setTab('edit')
    setMsg({ kind: 'info', text: `Loaded the values of version ${target.version} into the editor. Check what would change, then save.` })
  }

  return (
    <>
      <header className="page-head">
        <div>
          <h2>Rules &amp; thresholds</h2>
          <p>The cut-offs that turn test scores into decisions. Version {server.version || 'defaults'}
            {server.updated_at && <> · changed {fmtDate(server.updated_at)} by {server.updated_by || 'unknown'}</>}.</p>
        </div>
      </header>
      {msg && <Banner kind={msg.kind}>{msg.text}</Banner>}

      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === 'edit'} className={tab === 'edit' ? 'on' : ''} onClick={() => setTab('edit')}>Thresholds</button>
        <button role="tab" aria-selected={tab === 'history'} className={tab === 'history' ? 'on' : ''} onClick={() => setTab('history')}>Version history</button>
      </div>

      {tab === 'history' ? <History onLoad={loadVersion} onRestore={restore} busy={busy} current={server.version} /> : (
        <>
          {kpis && kpis.auto_decided > 0 && (
            <div className="evidence" role="note">
              <b>How the current rules are doing</b> (last 30 days):
              {' '}reviewers contradicted <b>{n(kpis.overturned)}</b> of {n(kpis.auto_decided)} automatic decisions ({pct(kpis.overturn_rate, 1)}),
              {' '}and <b>{pct(kpis.straight_through_rate)}</b> of videos were decided without a person.
              {kpis.overturn_rate > 0.1 && ' That is a lot of corrections: the thresholds may be too lenient or too strict.'}
              {' '}Change a value below to see what it would do to your past videos.
            </div>
          )}
          <div className="split">
            <div className="split-form">
              {GROUPS.map(([title, help, fields]) => (
                <section className="card form" key={title}>
                  <h3>{title}</h3>
                  <p className="sub group-help">{help}</p>
                  <div className="grid2">
                    {Object.entries(fields).map(([k, spec]) => {
                      const changed = k in draft
                      const dflt = server.defaults[k]
                      return spec[0] === 'bool' ? (
                        <label key={k} className={`check rule ${changed ? 'changed' : ''}`}>
                          <input type="checkbox" checked={!!values[k]} onChange={(e) => setValue(k, e.target.checked)} /> <span>{spec[1]}</span>
                          {changed && <span className="was">was {show(server.rules[k])}</span>}
                        </label>
                      ) : (
                        <label key={k} className={`rule ${changed ? 'changed' : ''}`}>{spec[0]}
                          <input type="number" step={spec[2]} min="0" value={values[k]} onChange={onNum(k)} onBlur={onBlurNum(k)} aria-invalid={draft[k] === ''} />
                          <span className="sub">{spec[1]}</span>
                          <span className="rule-meta sub">
                            {changed && <span className="was">was {server.rules[k]}</span>}
                            {values[k] !== dflt && !changed && <span>default {dflt}</span>}
                            {values[k] !== dflt && <button type="button" className="link-btn" onClick={() => setValue(k, dflt)}>Use default</button>}
                          </span>
                        </label>
                      )
                    })}
                  </div>
                </section>
              ))}
            </div>

            <aside className="split-side">
              {!keys.length && (
                <section className="card sticky hint-card">
                  <h3>Change a value to see its effect</h3>
                  <p className="sub">As soon as you edit a threshold, this panel re-judges your past videos with the new value and shows how many decisions would change, and how that compares with what reviewers decided.</p>
                  <p className="sub">Nothing is saved until you press <b>Save version</b>.</p>
                </section>
              )}
              {blank && <Banner kind="warn">Enter a number for every value you changed.</Banner>}
              {impactErr && <Banner kind="error">{impactErr}</Banner>}
              {impact && <Impact r={impact} days={days} setDays={setDays} loading={loading} />}
              {keys.length > 0 && !impact && !impactErr && !blank && <section className="card"><p className="sub">Working out the effect…</p></section>}
            </aside>
          </div>

          {keys.length > 0 && (
            <div className="save-bar" role="region" aria-label="Unsaved changes">
              <div className="sb-text">
                <b>{keys.length} unsaved change{keys.length === 1 ? '' : 's'}</b>
                <span className="sub">{keys.slice(0, 3).map((k) => `${label(k)}: ${show(server.rules[k])} → ${show(draft[k])}`).join(' · ')}{keys.length > 3 ? ` · +${keys.length - 3} more` : ''}</span>
              </div>
              <input ref={noteRef} className="sb-note" placeholder="Why? (kept in the history)" maxLength={300} value={note} onChange={(e) => setNote(e.target.value)} aria-label="Reason for the change" />
              <button onClick={() => { setDraft({}); setNote('') }} disabled={busy}>Discard</button>
              <button className="primary" onClick={save} disabled={busy || blank || !!impactErr}>Save version</button>
            </div>
          )}
        </>
      )}
    </>
  )
}
