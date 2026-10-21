import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { Banner, fmtDate } from '../components.jsx'

const visible = (f, values) => !f.show_if || Object.entries(f.show_if).every(([k, v]) => values[k] === v)
const defaults = (t) => Object.fromEntries(t.fields.filter((f) => !f.secret).map((f) => [f.key, f.default || '']))

/* One tab per connection type: a form on top, the saved connections below (edit / test / activate / delete). */
function ConnectionsTab({ t, meta, busy, run, setMsg }) {
  const [rows, setRows] = useState(null)
  const [editing, setEditing] = useState(null) // a saved connection, or null when adding
  const [name, setName] = useState('')
  const [values, setValues] = useState(defaults(t))
  const [secrets, setSecrets] = useState({})
  const [testMsg, setTestMsg] = useState(null)

  const load = useCallback(() => api.connections(t.type).then(setRows).catch((e) => setMsg({ kind: 'error', text: e.message })), [t.type, setMsg])
  useEffect(() => { load() }, [load])

  const reset = () => { setEditing(null); setName(''); setValues(defaults(t)); setSecrets({}); setTestMsg(null) }
  const edit = (r) => { setEditing(r); setName(r.name); setValues({ ...defaults(t), ...r.fields }); setSecrets({}); setTestMsg(null); window.scrollTo({ top: 0, behavior: 'smooth' }) }

  const payload = () => ({
    type: t.type, name, fields: values,
    secrets: Object.fromEntries(Object.entries(secrets).filter(([, v]) => v.trim())),
  })
  const save = () => run(async () => {
    if (editing) await api.updateConnection(editing.id, payload())
    else await api.createConnection(payload())
    setMsg({ kind: 'info', text: editing ? `Updated “${name}”.` : `Saved “${name}”.` })
    reset(); await load()
  })
  const test = () => run(async () => {
    const r = await api.testConnection({ ...payload(), id: editing?.id })
    setTestMsg({ kind: r.ok ? 'info' : 'error', text: r.message })
  })
  const testSaved = (r) => run(async () => {
    const res = await api.testConnection({ id: r.id, type: r.type })
    setMsg({ kind: res.ok ? 'info' : 'error', text: `${r.name}: ${res.message}` })
  })
  const toggle = (r) => run(async () => {
    await (r.active ? api.deactivateConnection(r.id) : api.activateConnection(r.id)); await load()
    setMsg({ kind: 'info', text: r.active ? `“${r.name}” deactivated${t.type === 'azure_blob' ? ' — new uploads stay on local disk' : ''}.` : `“${r.name}” is now the active ${t.label} connection.` })
  })
  const remove = (r) => {
    if (!window.confirm(`Delete “${r.name}”? This cannot be undone.`)) return
    run(async () => { await api.deleteConnection(r.id); if (editing?.id === r.id) reset(); await load(); setMsg({ kind: 'info', text: `Deleted “${r.name}”.` }) })
  }

  return (
    <>
      <p className="sub tab-desc">{t.description}</p>
      {!t.used && <Banner>This connection type is not used by the application yet. It can be stored now and will be picked up when the integration is added.</Banner>}

      <section className="card form">
        <h3>{editing ? `Edit “${editing.name}”` : 'Add connection'}</h3>
        <div className="grid2">
          <label>Name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Production" /></label>
          {t.fields.filter((f) => visible(f, values)).map((f) => f.secret ? (
            <label key={f.key}>{f.label}{f.required && !editing ? ' *' : ''}
              <input type="password" autoComplete="new-password" value={secrets[f.key] || ''} disabled={!meta.can_save_secret}
                onChange={(e) => setSecrets({ ...secrets, [f.key]: e.target.value })}
                placeholder={editing?.secrets[f.key]?.set
                  ? (editing.secrets[f.key].source === 'environment' ? 'Using the server’s environment variable — enter to override' : 'Saved — leave blank to keep')
                  : f.placeholder} />
              <span className="sub">{meta.can_save_secret ? 'Stored encrypted; never shown again.' : 'To save secrets here, set APP_SECRET_KEY on the server.'}</span>
            </label>
          ) : f.kind === 'select' ? (
            <label key={f.key}>{f.label}
              <select value={values[f.key]} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}>
                {f.options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
              {f.hint && <span className="sub">{f.hint}</span>}
            </label>
          ) : (
            <label key={f.key}>{f.label}{f.required ? ' *' : ''}
              <input value={values[f.key] || ''} placeholder={f.placeholder} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
              {f.hint && <span className="sub">{f.hint}</span>}
            </label>
          ))}
        </div>
        {testMsg && <Banner kind={testMsg.kind}>{testMsg.text}</Banner>}
        <div className="actions">
          <button onClick={test} disabled={busy}>Test connection</button>
          <button className="primary" onClick={save} disabled={busy || !name.trim()}>{editing ? 'Update' : 'Save'}</button>
          {editing && <button onClick={reset} disabled={busy}>Cancel</button>}
        </div>
        {t.type === 'azure_blob' && <p className="sub">“Test connection” writes, reads back and deletes a small test blob without saving anything.</p>}
      </section>

      <section className="card flush">
        <div className="card-head"><h3>Saved connections</h3></div>
        <table className="hover">
          <thead><tr><th>Name</th><th>Details</th><th>Status</th><th>Updated</th><th /></tr></thead>
          <tbody>
            {(rows || []).map((r) => (
              <tr key={r.id} className={editing?.id === r.id ? 'editing' : ''}>
                <td><b>{r.name}</b></td>
                <td className="mono-cell">{r.summary}{Object.values(r.secrets).some((s) => !s.set) && <div><span className="badge warn">secret missing</span></div>}</td>
                <td>{r.active ? <span className="badge good">{t.used ? 'In use' : 'Default'}</span> : <span className="badge muted">Inactive</span>}</td>
                <td>{fmtDate(r.updated_at)}</td>
                <td className="row-actions">
                  <button onClick={() => edit(r)} disabled={busy}>Edit</button>
                  <button onClick={() => testSaved(r)} disabled={busy}>Test</button>
                  <button onClick={() => toggle(r)} disabled={busy}>{r.active ? 'Deactivate' : 'Activate'}</button>
                  <button className="danger" onClick={() => remove(r)} disabled={busy || r.active} title={r.active ? 'Deactivate it first' : ''}>Delete</button>
                </td>
              </tr>
            ))}
            {rows && !rows.length && <tr><td colSpan="5" className="empty">No {t.label} connections saved yet.</td></tr>}
            {!rows && <tr><td colSpan="5" className="empty">Loading…</td></tr>}
          </tbody>
        </table>
      </section>
      {t.type === 'azure_blob' && <p className="sub">Only one blob connection is in use at a time; new uploads go to it. Videos already stored keep being read from the account they were saved to, so don’t delete a connection that still holds videos.</p>}
    </>
  )
}

export default function Configuration() {
  const [meta, setMeta] = useState(null)
  const [tab, setTab] = useState('azure_blob')
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => { api.connectionTypes().then(setMeta).catch((e) => setMsg({ kind: 'error', text: e.message })) }, [])

  const run = async (fn) => {
    setBusy(true); setMsg(null)
    try { await fn() } catch (e) { setMsg({ kind: 'error', text: e.message }) } finally { setBusy(false) }
  }
  const tabs = (meta?.types || []).map((t) => [t.type, t.label])
  const current = meta?.types.find((t) => t.type === tab)

  return (
    <>
      <header className="page-head"><div><h2>Configuration</h2><p>Connections to external services. Decision thresholds are under Rules &amp; thresholds.</p></div></header>
      <div className="tabs" role="tablist">
        {tabs.map(([k, l]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => { setTab(k); setMsg(null) }}>{l}</button>
        ))}
      </div>
      {msg && <Banner kind={msg.kind}>{msg.text}</Banner>}
      {current ? <ConnectionsTab key={current.type} t={current} meta={meta} busy={busy} run={run} setMsg={setMsg} />
        : !msg && <p>Loading…</p>}
    </>
  )
}
