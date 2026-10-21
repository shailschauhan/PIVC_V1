const GOOD = ['Match', 'Pass', 'Consistent', 'Accept', 'No indicators', 'Normal range', 'Estimated age']
const BAD = ['Mismatch', 'Fail', 'Suspected Manipulation', 'Inconsistent', 'Reject']

export function tone(status = '') {
  if (['Low signal quality', 'Low confidence', 'Not checked', 'Unavailable'].includes(status)) return 'muted'
  if (GOOD.includes(status) || status.startsWith('Estimated age')) return 'good'
  if (BAD.includes(status)) return 'bad'
  return 'warn' // Review, Flagged, Needs Review
}

export const Badge = ({ status }) =>
  status ? <span className={`badge ${tone(status)}`}>{status}</span> : <span className="badge muted">—</span>

export const pct = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`)

export const fmtDate = (d) => (d ? new Date(d).toLocaleString() : '—')

export function Confidence({ value }) {
  if (value == null) return <span className="muted-text">—</span>
  const t = value >= 0.85 ? 'good' : value >= 0.7 ? 'warn' : 'bad'
  return (
    <span className="conf">
      <span className="bar"><span className={t} style={{ width: `${value * 100}%` }} /></span>
      {pct(value)}
    </span>
  )
}

export function Stat({ label, value, hint, accent }) {
  return (
    <div className={`stat ${accent || ''}`}>
      <div className="stat-value">{value ?? '—'}</div>
      <div className="stat-label">{label}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  )
}

export function Banner({ children, kind = 'info' }) {
  return <div className={`banner ${kind}`}>{children}</div>
}

export const LABELS = {
  face_match: 'Face Match', liveness: 'Liveness', video_authenticity: 'Video Authenticity', speech: 'Speech',
  age_estimation: 'Age Estimation', information_consistency: 'Information Consistency',
  health_indicators: 'Health Indicators',
}
