import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { Banner } from '../components.jsx'

const EXT = /\.(mp4|mov|avi|mkv|webm|wmv)$/i
const MAX_MB = 500
const size = (b) => (b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`)

export default function Upload() {
  const nav = useNavigate()
  const [files, setFiles] = useState([])
  const [over, setOver] = useState(false)
  const [reference, setReference] = useState('')
  const [declaredAge, setDeclaredAge] = useState('')
  const [dob, setDob] = useState('')
  const [idDoc, setIdDoc] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState(null)

  const problem = (f) => (!EXT.test(f.name) ? 'Unsupported type' : f.size === 0 ? 'Empty file' : f.size > MAX_MB * 1048576 ? `Over ${MAX_MB} MB` : null)
  const add = (list) => setFiles((cur) => [...cur, ...Array.from(list).filter((f) => !cur.some((c) => c.name === f.name && c.size === f.size))])
  const valid = files.filter((f) => !problem(f))

  // One request per video, 3 at a time, so large batches never sit in a single request.
  const submit = async (e) => {
    e.preventDefault()
    setBusy(true); setErr('')
    const created = []
    const errors = []
    let done = 0, next = 0
    setProgress({ done: 0, total: valid.length })
    const worker = async () => {
      while (next < valid.length) {
        const f = valid[next++]
        const fd = new FormData()
        fd.append('files', f)
        if (reference) fd.append('reference', reference)
        if (declaredAge) fd.append('declared_age', declaredAge)
        if (dob) fd.append('dob', dob)
        if (idDoc) fd.append('id_document', idDoc)
        try { created.push(...(await api.upload(fd)).created) } catch (ex) { errors.push(`${f.name}: ${ex.message}`) }
        setProgress({ done: ++done, total: valid.length })
      }
    }
    await Promise.all([worker(), worker(), worker()])
    if (errors.length) setErr(errors.join(' · '))
    if (!created.length) return setBusy(false)
    if (!errors.length) nav(created.length === 1 ? `/cases/${created[0].id}` : '/cases')
    else { setBusy(false); setFiles(files.filter((f) => errors.some((m) => m.startsWith(`${f.name}:`)))) }
  }

  return (
    <>
      <header className="page-head">
        <div>
          <h2>Upload videos</h2>
          <p>Each video is checked for AI-generated or manipulated content. Results appear in All videos as soon as analysis finishes.</p>
        </div>
      </header>
      {err && <Banner kind="error">{err}</Banner>}
      <form className="card" onSubmit={submit}>
        <label className={`dropzone ${over ? 'over' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setOver(true) }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); add(e.dataTransfer.files) }}>
          <b>Drop videos here or click to browse</b>
          <div className="sub">MP4, MOV, AVI, MKV, WEBM, WMV · up to {MAX_MB} MB each · multiple files allowed</div>
          <input type="file" multiple accept="video/*,.mkv,.wmv" onChange={(e) => { add(e.target.files); e.target.value = '' }} />
        </label>
        {files.length > 0 && (
          <ul className="files">
            {files.map((f, i) => (
              <li key={i}>
                <span>{f.name}<span className="kind">{size(f.size)}</span>{problem(f) && <span className="badge bad" style={{ marginLeft: 8 }}>{problem(f)}</span>}</span>
                <button type="button" disabled={busy} onClick={() => setFiles(files.filter((_, j) => j !== i))}>Remove</button>
              </li>
            ))}
          </ul>
        )}
        <label>Reference (optional) — e.g. proposal number, applied to every video in this upload
          <input value={reference} onChange={(e) => setReference(e.target.value)} />
        </label>
        <div className="grid2">
          <label>Declared age (optional)
            <input type="number" min="0" max="120" value={declaredAge} onChange={(e) => setDeclaredAge(e.target.value)} placeholder="e.g. 34" />
            <span className="sub">The estimated age is compared with this; a large difference sends the video to review.</span>
          </label>
          <label>Date of birth (optional)
            <input type="date" value={dob} onChange={(e) => setDob(e.target.value)} />
            <span className="sub">Alternative to declared age. Applies to every video in this upload.</span>
          </label>
        </div>
        <label>ID document photo (optional)
          <input type="file" accept="image/jpeg,image/png,image/bmp,image/webp" onChange={(e) => setIdDoc(e.target.files[0] || null)} />
          <span className="sub">
            The photo from the customer's government ID (a clear picture of the document). When given, the face in the video is compared with it.
            Applies to every video in this upload{idDoc && idDoc.size > 10 * 1048576 ? ' — this file is over 10 MB and will be rejected' : ''}.
          </span>
        </label>
        {progress && busy && <div className="progress"><span style={{ width: `${(progress.done / progress.total) * 100}%` }} /></div>}
        <button className="primary" disabled={busy || !valid.length}>
          {busy ? `Uploading ${progress?.done ?? 0}/${progress?.total ?? 0}…` : `Upload & analyze ${valid.length || ''} video${valid.length === 1 ? '' : 's'}`}
        </button>
      </form>
    </>
  )
}
