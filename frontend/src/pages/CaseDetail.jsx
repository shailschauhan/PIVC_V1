import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { Badge, Banner, Confidence, fmtDate } from '../components.jsx'
import { useAuth } from '../auth.jsx'

/* Per-frame manipulation score; clicking a bar seeks the video to that moment. */
function FrameStrip({ rows, flag, onSeek }) {
  if (!rows?.length) return null
  const W = 480, H = 56, w = W / rows.length
  return (
    <svg className="spark" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Per-frame manipulation score">
      <line x1="0" x2={W} y1={H * (1 - flag)} y2={H * (1 - flag)} stroke="var(--muted)" strokeDasharray="3 3" />
      {rows.map((r, i) => <rect key={`t${i}`} x={i * w + 1} y="0" width={Math.max(w - 2, 1)} height={H} rx="2" fill="var(--line)" opacity="0.5" />)}
      {rows.map((r, i) => (
        <rect key={i} x={i * w + 1} y={H - r.score * H} width={Math.max(w - 2, 1)} height={Math.max(r.score * H, 1)} rx="2"
          fill={r.score >= flag ? 'var(--bad)' : 'var(--brand)'} style={{ cursor: r.seconds != null ? 'pointer' : 'default' }}
          onClick={() => r.seconds != null && onSeek(r.seconds)}>
          <title>{`frame ${r.frame}${r.seconds != null ? ` (${r.seconds}s)` : ''}: ${r.score}`}</title>
        </rect>
      ))}
    </svg>
  )
}

/* Blood-volume-pulse waveform (downsampled) */
function Wave({ values }) {
  if (!values?.length) return null
  const W = 480, H = 56
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * W},${H - 4 - ((v - min) / span) * (H - 8)}`).join(' ')
  return (
    <svg className="spark" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Pulse waveform">
      <polyline points={pts} fill="none" stroke="var(--brand)" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  )
}

const Metric = ({ label, value, unit, hint }) => (
  <div className="metric"><div className="label">{label}</div><div className="metric-v">{value ?? '—'}{value != null && unit ? <small> {unit}</small> : null}</div>{hint && <div className="sub">{hint}</div>}</div>
)

function Health({ h, rules }) {
  const d = h.details
  if (d.error) return <p className="sub">{d.error}</p>
  const v = d.video || {}
  const hrv = d.hrv || {}
  return (
    <>
      {!d.reliable && (
        <Banner kind="warn">Not reliable for this video: {d.not_reliable_because.join('; ')}. The numbers below are shown for transparency only and are not used.</Banner>
      )}
      {h.exceptions.length > 0 && <ul className="plain">{h.exceptions.map((e) => <li key={e}>{e}</li>)}</ul>}
      <div className="metrics">
        <Metric label="Heart rate" value={d.hr_bpm != null ? Math.round(d.hr_bpm) : null} unit="bpm" hint={`normal ${rules.hr_normal_low}–${rules.hr_normal_high}`} />
        <Metric label="Breathing" value={d.breathing_rate_bpm != null ? Math.round(d.breathing_rate_bpm) : null} unit="/min" hint={`normal ${rules.resp_normal_low}–${rules.resp_normal_high}`} />
        <Metric label="Signal quality" value={d.sqi != null ? d.sqi.toFixed(2) : null} hint={`min ${rules.health_sqi_min}`} />
        <Metric label="Measured over" value={d.duration_s} unit="s" hint={`min ${rules.health_min_seconds}s`} />
        {hrv.sdnn_ms != null && <Metric label="HRV SDNN" value={hrv.sdnn_ms.toFixed(0)} unit="ms" />}
        {hrv.rmssd_ms != null && <Metric label="HRV RMSSD" value={hrv.rmssd_ms.toFixed(0)} unit="ms" />}
        {hrv.pnn50 != null && <Metric label="pNN50" value={(hrv.pnn50 * 100).toFixed(0)} unit="%" />}
        {hrv.lf_hf != null && <Metric label="LF/HF" value={hrv.lf_hf.toFixed(2)} />}
      </div>
      <Wave values={d.bvp} />
      <p className="sub">
        Pulse waveform estimated from the face. Video: {v.frames} frames at {v.fps} fps, {v.non_key_fraction != null ? `${Math.round(v.non_key_fraction * 100)}% non-key (compressed) frames` : ''}
        {v.no_face_frames ? `, ${v.no_face_frames} without a face` : ''}.
      </p>
      <p className="sub">{d.limitations.join(' ')} {rules.health_flag_requires_review ? 'A flagged reading sends the video to manual review.' : 'This is reported for information and does not change the decision.'}</p>
    </>
  )
}

/* Per-frame age estimates for the main face; click a point to jump to that moment. */
function AgeStrip({ rows, est, onSeek }) {
  const pts = (rows || []).filter((r) => r.age != null)
  if (!pts.length) return null
  const W = 480, H = 70
  const lo = Math.max(0, Math.min(...pts.map((r) => r.age), est ?? 99) - 8)
  const hi = Math.min(110, Math.max(...pts.map((r) => r.age), est ?? 0) + 8)
  const y = (a) => H - 6 - ((a - lo) / (hi - lo || 1)) * (H - 12)
  return (
    <svg className="spark" style={{ height: 70 }} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Estimated age per sampled frame">
      {est != null && <line x1="0" x2={W} y1={y(est)} y2={y(est)} stroke="var(--muted)" strokeDasharray="3 3" />}
      {pts.map((r, i) => (
        <circle key={i} cx={(i + 0.5) * (W / pts.length)} cy={y(r.age)} r="5" fill="var(--brand)" stroke="var(--surface)" strokeWidth="2"
          style={{ cursor: r.seconds != null ? 'pointer' : 'default' }} onClick={() => r.seconds != null && onSeek(r.seconds)}>
          <title>{`frame ${r.frame}${r.seconds != null ? ` (${r.seconds}s)` : ''}: ${r.age} years`}</title>
        </circle>
      ))}
      <text x="2" y="10" className="axis" fill="var(--muted)" fontSize="10">{Math.round(hi)} yrs</text>
      <text x="2" y={H - 1} className="axis" fill="var(--muted)" fontSize="10">{Math.round(lo)} yrs</text>
    </svg>
  )
}

/* Per-frame similarity to the ID photo with the two decision thresholds; frames with quality problems are drawn lighter. */
function SimStrip({ rows, match, mismatch, onSeek }) {
  const pts = (rows || []).filter((r) => r.similarity != null)
  if (!pts.length) return null
  const W = 480, H = 70, bw = W / pts.length
  const y = (v) => H - 4 - Math.max(0, Math.min(1, v)) * (H - 8)
  return (
    <svg className="spark" style={{ height: 70 }} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Similarity to the ID photo per sampled frame">
      {[match, mismatch].map((t) => <line key={t} x1="0" x2={W} y1={y(t)} y2={y(t)} stroke="var(--muted)" strokeDasharray="3 3" />)}
      {pts.map((r, i) => (
        <rect key={i} x={i * bw + 1} y={y(r.similarity)} width={Math.max(bw - 2, 1)} height={Math.max(H - 4 - y(r.similarity), 1)} rx="2"
          fill={r.similarity >= match ? 'var(--good)' : r.similarity < mismatch ? 'var(--bad)' : 'var(--warn)'}
          opacity={r.quality_flags?.length ? 0.45 : 1} style={{ cursor: r.seconds != null ? 'pointer' : 'default' }} onClick={() => r.seconds != null && onSeek(r.seconds)}>
          <title>{`frame ${r.frame}${r.seconds != null ? ` (${r.seconds}s)` : ''}: similarity ${r.similarity}${r.quality_flags?.length ? ` — ${r.quality_flags.join(', ')}` : ''}`}</title>
        </rect>
      ))}
    </svg>
  )
}

function FaceBlock({ f, rules, caseId, hasId, canViewId, onSeek }) {
  const d = f.details
  if (d.error) return <p className="sub">{d.error}</p>
  if (d.skipped) return <p className="sub">{d.skipped} Upload an ID document photo together with the video to run this test.</p>
  const ref = d.reference || {}
  return (
    <div className="face-grid">
      <div>
        {f.exceptions.length > 0 && <ul className="plain">{f.exceptions.map((e) => <li key={e}>{e}</li>)}</ul>}
        {d.low_quality && <Banner kind="warn">Image or video quality is poor, so a mismatch cannot be concluded; this goes to review.</Banner>}
        <div className="metrics">
          <Metric label="Similarity (median)" value={d.similarity != null ? d.similarity.toFixed(2) : null} hint={`match ≥ ${rules.face_match_threshold}, mismatch < ${rules.face_mismatch_threshold}`} />
          <Metric label="Frames matching" value={d.fraction_frames_matching != null ? `${Math.round(d.fraction_frames_matching * 100)}%` : null} />
          <Metric label="Frames used" value={`${d.frames_usable ?? 0}/${d.frames_sampled}`} hint={`${d.frames_with_face} with a face`} />
          <Metric label="ID photo faces" value={ref.faces_found} hint={ref.quality?.flags?.length ? ref.quality.flags.join(', ') : 'quality ok'} />
        </div>
        <SimStrip rows={d.frames} match={rules.face_match_threshold} mismatch={rules.face_mismatch_threshold} onSeek={onSeek} />
        <p className="sub">{d.reason} Each bar is one sampled frame (dashed lines = thresholds; faded = quality problem); click to jump to it. {d.limitations.join(' ')}</p>
      </div>
      {hasId && canViewId && (
        <figure className="id-photo">
          <img src={`/api/cases/${caseId}/id-document`} alt="ID document photo supplied with this video" />
          <figcaption>ID document photo</figcaption>
        </figure>
      )}
    </div>
  )
}

function LivenessBlock({ l, rules, onSeek }) {
  const d = l.details
  if (d.error) return <p className="sub">{d.error}</p>
  const rows = (d.frames || []).filter((r) => r.real_probability != null)
  const W = 480, H = 70, bw = rows.length ? W / rows.length : W
  const y = (v) => H - 4 - v * (H - 8)
  const unjudgeable = rows.filter((r) => (r.flags || []).some((x) => x === 'too_small' || x === 'tight_framing')).length
  return (
    <>
      <Banner kind="warn">A passive risk signal, not proof of liveness. It cannot detect deepfakes and has not been measured on real attacks; it never rejects a video on its own.</Banner>
      {l.exceptions.length > 0 && <ul className="plain">{l.exceptions.map((e) => <li key={e}>{e}</li>)}</ul>}
      <div className="metrics">
        <Metric label="Real-face probability" value={d.real_probability != null ? d.real_probability.toFixed(2) : null} hint={`pass ≥ ${rules.liveness_pass_threshold}, suspect < ${rules.liveness_spoof_threshold}`} />
        <Metric label="Frames judged" value={`${d.frames_usable ?? 0}/${d.frames_sampled}`} hint={unjudgeable ? `${unjudgeable} too close-up or small to judge` : null} />
        <Metric label="Frames real" value={d.fraction_frames_real != null ? `${Math.round(d.fraction_frames_real * 100)}%` : null} />
      </div>
      {rows.length > 0 && (
        <svg className="spark" style={{ height: 70 }} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Real-face probability per sampled frame">
          {[rules.liveness_pass_threshold, rules.liveness_spoof_threshold].map((t) => <line key={t} x1="0" x2={W} y1={y(t)} y2={y(t)} stroke="var(--muted)" strokeDasharray="3 3" />)}
          {rows.map((r, i) => {
            const bad = (r.flags || []).some((x) => x === 'too_small' || x === 'tight_framing')
            return (
              <rect key={i} x={i * bw + 1} y={y(r.real_probability)} width={Math.max(bw - 2, 1)} height={Math.max(H - 4 - y(r.real_probability), 1)} rx="2"
                fill={r.real_probability >= rules.liveness_pass_threshold ? 'var(--good)' : r.real_probability < rules.liveness_spoof_threshold ? 'var(--bad)' : 'var(--warn)'}
                opacity={bad ? 0.4 : 1} style={{ cursor: r.seconds != null ? 'pointer' : 'default' }} onClick={() => r.seconds != null && onSeek(r.seconds)}>
                <title>{`frame ${r.frame}${r.seconds != null ? ` (${r.seconds}s)` : ''}: real ${r.real_probability}${r.flags?.length ? ` — ${r.flags.join(', ')} (not judged)` : ''}`}</title>
              </rect>
            )
          })}
        </svg>
      )}
      <p className="sub">{d.reason} Faded bars are frames the model cannot judge. {d.limitations.join(' ')}</p>
    </>
  )
}

function AgeBlock({ a, rules, onSeek }) {
  const d = a.details
  if (d.error) return <p className="sub">{d.error}</p>
  return (
    <>
      {!d.reliable && <Banner kind="warn">Not reliable for this video: {d.not_reliable_because.join('; ')}. The number below is shown for transparency only.</Banner>}
      {a.exceptions.length > 0 && <ul className="plain">{a.exceptions.map((e) => <li key={e}>{e}</li>)}</ul>}
      <div className="metrics">
        <Metric label="Estimated age" value={d.age != null ? Math.round(d.age) : null} unit="yrs" hint={d.age_low != null ? `typical range ${Math.round(d.age_low)}–${Math.round(d.age_high)}` : null} />
        <Metric label="Declared age" value={d.declared_age} unit="yrs" hint={d.declared_age == null ? 'not provided' : `tolerance ±${rules.age_tolerance_years}`} />
        <Metric label="Difference" value={d.difference != null ? `${d.difference > 0 ? '+' : ''}${Math.round(d.difference)}` : null} unit="yrs" />
        <Metric label="Frames used" value={`${d.frames_aged}/${d.frames_sampled}`} hint={d.spread != null ? `spread ${d.spread} yrs` : null} />
      </div>
      <AgeStrip rows={d.frame_results} est={d.age} onSeek={onSeek} />
      {d.multi_face_fraction > 0.3 && (
        <Banner kind="warn">More than one face was visible in {Math.round(d.multi_face_fraction * 100)}% of the sampled frames. The age shown is for the largest face; check that nobody else is in the call.</Banner>
      )}
      <p className="sub">Each dot is one sampled frame (dashed line = the median estimate); click one to jump to that moment. {d.limitations.join(' ')} {rules.age_mismatch_requires_review ? 'A mismatch sends the video to manual review.' : 'A mismatch is reported only.'}</p>
    </>
  )
}

const LEVEL = { ok: 'good', attention: 'warn', info: 'muted' }

/* The written explanation of the results. Advisory only: it never changes the decision. */
function Interpretation({ c, busy, onWrite, canWrite }) {
  const ai = c.interpretation
  return (
    <section className="card ai-card">
      <div className="ai-head">
        <h3>Interpretation <span className="tag">AI-written</span></h3>
        {canWrite && c.status === 'ANALYZED' && (
          <button onClick={onWrite} disabled={busy}>{busy ? 'Writing…' : ai ? (ai.stale ? 'Update for the new results' : 'Write again') : 'Write an interpretation'}</button>
        )}
      </div>
      {!ai && !busy && <p className="sub">A plain-language explanation of what the tests found and what to look at. It is written from the results only (never the video or any personal details) and does not change the decision.</p>}
      {busy && !ai && <p className="sub">Writing the interpretation… this takes a few seconds.</p>}
      {ai && (
        <>
          {ai.stale && <Banner kind="warn">Some tests were re-run after this was written, so it may not match the results shown. Update it to refresh.</Banner>}
          <p className="ai-headline">{ai.headline}</p>
          <p>{ai.summary}</p>
          {ai.findings.length > 0 && (
            <ul className="ai-findings">
              {ai.findings.map((f, i) => <li key={i} className={LEVEL[f.level] || 'muted'}><i aria-hidden="true" /><span><b>{f.test}</b> {f.text}</span></li>)}
            </ul>
          )}
          <div className="ai-cols">
            {[['To look at', ai.concerns], ['Suggested next steps', ai.next_steps], ['What these results cannot tell', ai.caveats]].filter(([, l]) => l.length).map(([t, l]) => (
              <div key={t}><h4>{t}</h4><ul className="plain">{l.map((x) => <li key={x}>{x}</li>)}</ul></div>
            ))}
          </div>
          <p className="sub ai-foot">Written by an AI assistant {ai.generated_at ? `on ${fmtDate(ai.generated_at)} ` : ''}from the results on this page. It explains them; it is not a further test, and the reviewer decides.</p>
        </>
      )}
    </section>
  )
}

export default function CaseDetail() {
  const { id } = useParams()
  const [c, setC] = useState(null)
  const [err, setErr] = useState('')
  const [notes, setNotes] = useState('')
  const { can, user } = useAuth()
  const [busy, setBusy] = useState(false)
  const video = useRef(null)
  const [aiOn, setAiOn] = useState(false)
  const [aiBusy, setAiBusy] = useState(false)
  useEffect(() => { api.aiStatus().then((s) => setAiOn(s.configured)).catch(() => {}) }, [])

  const load = useCallback(() => api.case(id).then(setC).catch((e) => setErr(e.message)), [id])
  useEffect(() => {
    load()
    const t = setInterval(() => { if (!c || c.status === 'PENDING' || c.status === 'PROCESSING') load() }, 2000)
    return () => clearInterval(t)
  }, [load, c?.status]) // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (fn) => {
    setBusy(true); setErr('')
    try { setC(await fn()) } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  const decide = (d) => {
    act(() => api.review(c.id, d, notes))
  }
  const interpret = async () => {
    setAiBusy(true); setErr('')
    try { await api.interpret(c.id); setC(await api.case(c.id)) } catch (e) { setErr(e.message) } finally { setAiBusy(false) }
  }
  const seek = (s) => { if (video.current) { video.current.currentTime = s; video.current.play() } }
  // re-run one test: the other tests keep their results and the overall decision is recomputed
  const rerun = (key, label) => {
    const warn = c.review_status === 'COMPLETED' ? '\n\nA completed human review is kept unless this changes the overall decision.' : ''
    if (!window.confirm(`Re-run ${label} only?\n\nThe other tests keep their results; the overall decision is recomputed from all of them.${warn}`)) return
    act(async () => { await api.analyze(c.id, [key]); return api.case(c.id) })
  }
  const Rerun = ({ k, label }) => can('reviewer') && !working
    ? <button type="button" className="rerun-btn" onClick={() => rerun(k, label)} disabled={busy} title={`Run only ${label} again`}>↻ Re-run this test</button> : null

  if (!c) return err ? <Banner kind="error">{err}</Banner> : <p>Loading…</p>
  const r = c.report
  const p = r?.parameters.video_authenticity
  const d = p?.details
  const flag = r?.rules_applied.fake_score_flag ?? 0.3
  const working = c.status === 'PENDING' || c.status === 'PROCESSING'

  return (
    <>
      <Link to="/cases" className="back">← All videos</Link>
      <header className="page-head">
        <div>
          <h2>{c.filename}</h2>
          <p>{c.case_ref}{c.reference ? ` · ${c.reference}` : ''}</p>
        </div>
        <div className="actions">
          {can('reviewer') && <button onClick={() => act(async () => { await api.analyze(c.id); return api.case(c.id) })} disabled={busy}>Re-run all tests</button>}
          {r && <a className="button primary" href={`/api/cases/${c.id}/report.pdf`}>Download PDF report</a>}
          {r && <a className="button" href={`/api/cases/${c.id}/report`} title="All the data behind the report">Data (JSON)</a>}
        </div>
      </header>
      {err && <Banner kind="error">{err}</Banner>}
      {c.status === 'FAILED' && <Banner kind="error">Analysis failed: {c.error}</Banner>}
      {(c.status === 'PENDING' || c.status === 'PROCESSING') && <Banner>Analysis in progress…</Banner>}

      <div className="grid-main">
        <section className="card">
          <video ref={video} className="player" controls preload="metadata" src={`/api/cases/${c.id}/video`} />
        </section>
        {r && (
          <section className="card">
            <div className="overall stack">
              <div><div className="label">Decision</div><Badge status={c.decision} /></div>
              <div><div className="label">Video authenticity</div><Badge status={p.status} /></div>
              <div><div className="label">Confidence</div><Confidence value={c.confidence} /></div>
              <div><div className="label">Final decision</div>
                {c.final_decision ? <Badge status={c.final_decision} /> : <span className="badge warn">Awaiting review</span>}</div>
              <div><div className="label">Processing time</div>{c.processing_ms} ms</div>
            </div>
            {r.exception_details.length > 0 && (
              <>
                <h3>Why it was flagged</h3>
                <ul className="plain">{r.exception_details.map((e) => <li key={e}>{e}</li>)}</ul>
              </>
            )}
          </section>
        )}
      </div>

      {r && (aiOn || c.interpretation) && <Interpretation c={c} busy={aiBusy} onWrite={interpret} canWrite={can('reviewer')} />}

      {r && (
        <>
          <section className="card">
            <h3>Evidence <span className="tag">{p.engine}</span> <Rerun k="video_authenticity" label="Video authenticity" /></h3>
            {d.error ? <p>{d.error}</p> : (
              <>
                <p className="sub">
                  Fake score <b>{d.fake_score ?? '—'}</b> (review ≥ {r.rules_applied.fake_score_review}, flag ≥ {flag}) · {d.models.join(' + ')} ({d.dataset}, {d.device}) ·
                  {' '}{d.frames_with_face}/{d.frames_sampled} sampled frames contain a face · {d.suspicious_frames} suspicious
                </p>
                {Object.keys(d.model_scores).length > 1 && (
                  <p className="sub">{Object.entries(d.model_scores).map(([m, v]) => `${m} ${v}`).join(' · ')}</p>
                )}
                {d.background?.virtual_suspected && (
                  <Banner kind="warn">
                    Possible {d.background.reasons.join(' / ')} (e.g. Teams/Zoom effect). Segmentation edges can resemble manipulation,
                    so a manipulation flag on this video needs human confirmation.
                  </Banner>
                )}
                {d.background && (
                  <p className="sub">
                    Background: {d.background.assessed
                      ? `${d.background.virtual_suspected ? 'virtual/blurred suspected' : 'looks real'} (sharpness ratio ${d.background.blur_ratio}, frame change ${d.background.static_diff ?? '—'})`
                      : 'not assessed (face fills the frame)'}
                  </p>
                )}
                <FrameStrip rows={d.frame_results} flag={flag} onSeek={seek} />
                <p className="sub">Each bar is one sampled frame (full height = score 1.0, dashed line = flag threshold); click one to jump to that moment. {d.limitations.join(' ')}</p>
              </>
            )}
          </section>

          {r.parameters.health_indicators && (
            <section className="card">
              <h3>{r.labels.health_indicators} <span className="tag">{r.parameters.health_indicators.engine}</span> <Badge status={r.parameters.health_indicators.status} /> <Rerun k="health_indicators" label="Health indicators" /></h3>
              <Health h={r.parameters.health_indicators} rules={r.rules_applied} />
            </section>
          )}

          {r.parameters.face_match && (
            <section className="card">
              <h3>{r.labels.face_match} <span className="tag">{r.parameters.face_match.engine}</span> <Badge status={r.parameters.face_match.status} /> <Rerun k="face_match" label="Face match" /></h3>
              <FaceBlock f={r.parameters.face_match} rules={r.rules_applied} caseId={c.id} hasId={c.has_id_document} canViewId={can('reviewer')} onSeek={seek} />
            </section>
          )}

          {r.parameters.liveness && (
            <section className="card">
              <h3>{r.labels.liveness} <span className="tag">{r.parameters.liveness.engine}</span> <Badge status={r.parameters.liveness.status} /> <Rerun k="liveness" label="Liveness" /></h3>
              <LivenessBlock l={r.parameters.liveness} rules={r.rules_applied} onSeek={seek} />
            </section>
          )}

          {r.parameters.age_estimation && (
            <section className="card">
              <h3>{r.labels.age_estimation} <span className="tag">{r.parameters.age_estimation.engine}</span> <Badge status={r.parameters.age_estimation.status} /> <Rerun k="age_estimation" label="Age estimation" /></h3>
              <AgeBlock a={r.parameters.age_estimation} rules={r.rules_applied} onSeek={seek} />
            </section>
          )}

          <div className="grid2">
            <section className="card">
              <h3>Human review</h3>
              {c.review_status === 'COMPLETED' && (
                <p>Final decision <Badge status={c.final_decision} /> by <b>{c.reviewer}</b> on {fmtDate(c.reviewed_at)}.
                  {c.review_notes && <><br />Notes: {c.review_notes}</>}</p>
              )}
              {c.review_status === 'NOT_REQUIRED' && <p className="muted-text">Auto-decided with sufficient confidence. You can still override below.</p>}
              {c.review_status === 'PENDING' && <Banner kind="warn">Manual verification required.</Banner>}
              {can('reviewer') ? (
                <>
                  <p className="sub">Recorded as <b>{user.full_name}</b> ({user.username}).</p>
                  <label>Notes<textarea rows="3" value={notes} onChange={(e) => setNotes(e.target.value)} /></label>
                  <div className="actions">
                    <button className="good" disabled={busy} onClick={() => decide('Accept')}>Accept</button>
                    <button className="bad" disabled={busy} onClick={() => decide('Reject')}>Reject</button>
                  </div>
                </>
              ) : <p className="sub">Your role is read-only, so you cannot record a decision.</p>}
            </section>
            <section className="card">
              <h3>Audit trail</h3>
              <ul className="plain">
                {c.audit.map((a, i) => (
                  <li key={i}>{fmtDate(a.at)} — <b>{a.actor}</b> {a.action}
                    {a.detail?.final_decision && ` → ${a.detail.final_decision}`}</li>
                ))}
              </ul>
            </section>
          </div>
        </>
      )}
    </>
  )
}
