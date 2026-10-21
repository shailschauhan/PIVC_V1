import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { Banner, fmtDate } from '../components.jsx'
import { ROLE_HELP, ROLE_LABEL, useAuth } from '../auth.jsx'

const ROLES = ['viewer', 'reviewer', 'admin']
const roleTone = { viewer: 'muted', reviewer: 'info', admin: 'warn' }

// 14 readable characters (no look-alikes), grouped for easy reading out loud: e.g. "Kf7m-Qx3n-Rt8w"
function generatePassword() {
  const a = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
  const bytes = crypto.getRandomValues(new Uint8Array(12))
  const c = [...bytes].map((b) => a[b % a.length])
  return `${c.slice(0, 4).join('')}-${c.slice(4, 8).join('')}-${c.slice(8, 12).join('')}`
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); return true } catch { return false }
}

export default function Users() {
  const { user: me } = useAuth()
  const [rows, setRows] = useState(null)
  const [mode, setMode] = useState({ kind: 'add' })            // add | edit | reset
  const [f, setF] = useState({ username: '', full_name: '', role: 'viewer', password: '' })
  const [show, setShow] = useState(false)
  const [msg, setMsg] = useState(null)
  const [issued, setIssued] = useState(null)                    // temporary password to hand over, shown once
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => api.users().then(setRows).catch((e) => setMsg({ kind: 'error', text: e.message })), [])
  useEffect(() => { load() }, [load])

  const reset = () => { setMode({ kind: 'add' }); setF({ username: '', full_name: '', role: 'viewer', password: '' }); setShow(false) }
  const edit = (u) => { setMode({ kind: 'edit', user: u }); setF({ username: u.username, full_name: u.full_name, role: u.role, password: '' }); setIssued(null); window.scrollTo({ top: 0, behavior: 'smooth' }) }
  const startReset = (u) => { setMode({ kind: 'reset', user: u }); setF({ ...f, username: u.username, full_name: u.full_name, password: generatePassword() }); setShow(true); setIssued(null); window.scrollTo({ top: 0, behavior: 'smooth' }) }

  const run = async (fn) => {
    setBusy(true); setMsg(null)
    try { await fn() } catch (e) { setMsg({ kind: 'error', text: e.message }) } finally { setBusy(false) }
  }

  const submit = (e) => {
    e.preventDefault()
    run(async () => {
      if (mode.kind === 'add') {
        await api.createUser(f)
        setIssued({ username: f.username.trim().toLowerCase(), password: f.password })
        setMsg({ kind: 'info', text: `Created ${f.username}. They must choose their own password at first sign-in.` })
      } else if (mode.kind === 'edit') {
        await api.updateUser(mode.user.id, { full_name: f.full_name, role: f.role })
        setMsg({ kind: 'info', text: `Updated ${mode.user.username}.` })
      } else {
        await api.resetPassword(mode.user.id, f.password)
        setIssued({ username: mode.user.username, password: f.password })
        setMsg({ kind: 'info', text: `Password reset for ${mode.user.username}. They were signed out and must choose a new password.` })
      }
      reset(); await load()
    })
  }

  const toggleActive = (u) => run(async () => {
    await api.updateUser(u.id, { active: !u.active }); await load()
    setMsg({ kind: 'info', text: `${u.username} ${u.active ? 'deactivated and signed out' : 'reactivated'}.` })
  })
  const unlock = (u) => run(async () => { await api.unlockUser(u.id); await load(); setMsg({ kind: 'info', text: `${u.username} unlocked.` }) })
  const remove = (u) => {
    if (!window.confirm(`Delete the account “${u.username}”?\n\nThey can no longer sign in. Their past activity stays in the audit trail under their username.`)) return
    run(async () => { await api.deleteUser(u.id); await load(); setMsg({ kind: 'info', text: `Deleted ${u.username}.` }) })
  }

  const adding = mode.kind === 'add', editing = mode.kind === 'edit', resetting = mode.kind === 'reset'
  const title = adding ? 'Add user' : editing ? `Edit ${mode.user.username}` : `Reset password for ${mode.user.username}`

  return (
    <>
      <header className="page-head"><div><h2>User management</h2><p>Who can sign in, and what they are allowed to do.</p></div></header>
      {msg && <Banner kind={msg.kind}>{msg.text}</Banner>}
      {issued && (
        <Banner kind="warn">
          Temporary password for <b>{issued.username}</b>: <code className="temp-pw">{issued.password}</code>{' '}
          <button className="link" onClick={async () => setMsg({ kind: 'info', text: (await copy(issued.password)) ? 'Copied to the clipboard.' : 'Could not copy automatically — select and copy it.' })}>Copy</button>
          <button className="link" onClick={() => setIssued(null)}>Dismiss</button>
          <div className="sub">Share it with them securely. It is shown only now and they must change it at first sign-in.</div>
        </Banner>
      )}

      <form className="card form" onSubmit={submit}>
        <h3>{title}</h3>
        <div className="grid2">
          <label>Username<input value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} disabled={!adding} autoComplete="off" required
            placeholder="e.g. priya.sharma" /><span className="sub">3–40 characters: letters, numbers, . _ @ -</span></label>
          {!resetting && <label>Full name<input value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} autoComplete="off" /></label>}
          {!resetting && (
            <label>Role
              <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })} disabled={editing && mode.user.id === me.id}>
                {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
              </select>
              <span className="sub">{ROLE_HELP[f.role]}</span>
            </label>
          )}
          {!editing && (
            <label>{resetting ? 'New temporary password' : 'Temporary password'}
              <span className="pw-row">
                <input type={show ? 'text' : 'password'} value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} autoComplete="new-password" required />
                <button type="button" onClick={() => setShow(!show)}>{show ? 'Hide' : 'Show'}</button>
                <button type="button" onClick={() => { setF({ ...f, password: generatePassword() }); setShow(true) }}>Generate</button>
              </span>
              <span className="sub">At least 10 characters. The user must change it at first sign-in.</span>
            </label>
          )}
        </div>
        <div className="actions">
          <button className="primary" disabled={busy || !f.username || (!editing && !f.password)}>{adding ? 'Add user' : editing ? 'Save changes' : 'Reset password'}</button>
          {!adding && <button type="button" onClick={reset} disabled={busy}>Cancel</button>}
        </div>
      </form>

      <section className="card flush">
        <div className="card-head"><h3>Users</h3><span className="sub">{rows ? `${rows.length} account${rows.length === 1 ? '' : 's'}` : ''}</span></div>
        <table className="hover">
          <thead><tr><th>User</th><th>Role</th><th>Status</th><th>Last sign-in</th><th /></tr></thead>
          <tbody>
            {(rows || []).map((u) => {
              const self = u.id === me.id
              return (
                <tr key={u.id} className={mode.user?.id === u.id ? 'editing' : ''}>
                  <td><b>{u.full_name}</b>{self && <span className="tag">you</span>}<div className="sub">{u.username}</div></td>
                  <td><span className={`badge ${roleTone[u.role]}`}>{ROLE_LABEL[u.role]}</span></td>
                  <td>
                    {u.active ? <span className="badge good">Active</span> : <span className="badge muted">Deactivated</span>}
                    {u.locked && <span className="badge bad" style={{ marginLeft: 6 }}>Locked</span>}
                    {u.must_change_password && <div className="sub">must change password</div>}
                  </td>
                  <td>{u.last_login_at ? fmtDate(u.last_login_at) : <span className="muted-text">never</span>}</td>
                  <td className="row-actions">
                    <button onClick={() => edit(u)} disabled={busy}>Edit</button>
                    <button onClick={() => startReset(u)} disabled={busy}>Reset password</button>
                    {u.locked && <button onClick={() => unlock(u)} disabled={busy}>Unlock</button>}
                    <button onClick={() => toggleActive(u)} disabled={busy || self} title={self ? 'You cannot deactivate yourself' : ''}>{u.active ? 'Deactivate' : 'Activate'}</button>
                    <button className="danger" onClick={() => remove(u)} disabled={busy || self} title={self ? 'You cannot delete yourself' : ''}>Delete</button>
                  </td>
                </tr>
              )
            })}
            {rows && !rows.length && <tr><td colSpan="5" className="empty">No users.</td></tr>}
            {!rows && <tr><td colSpan="5" className="empty">Loading…</td></tr>}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h3>What each role can do</h3>
        <table className="mini roles">
          <thead><tr><th /><th>Viewer</th><th>Reviewer</th><th>Administrator</th></tr></thead>
          <tbody>
            {[['See dashboard, videos, reports', 1, 1, 1], ['Upload videos, re-run tests', 0, 1, 1], ['Record review decisions (Accept / Reject)', 0, 1, 1],
              ['Delete videos', 0, 0, 1], ['Change thresholds and connections', 0, 0, 1], ['Manage users', 0, 0, 1]].map(([label, ...v]) => (
              <tr key={label}><td>{label}</td>{v.map((x, i) => <td key={i} className="center">{x ? '✓' : <span className="muted-text">—</span>}</td>)}</tr>
            ))}
          </tbody>
        </table>
        <p className="sub">There is always at least one active administrator: the last one cannot be deactivated, demoted or deleted. Permissions are enforced by the server, not just hidden in the menu.</p>
      </section>
    </>
  )
}
