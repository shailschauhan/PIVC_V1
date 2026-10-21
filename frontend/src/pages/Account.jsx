import { useState } from 'react'
import { api } from '../api.js'
import { Banner } from '../components.jsx'
import { ROLE_HELP, ROLE_LABEL, useAuth } from '../auth.jsx'
import { IconLogo } from '../icons.jsx'

export function ChangePasswordForm({ forced = false }) {
  const { refresh, user } = useAuth()
  const [f, setF] = useState({ current: '', next: '', confirm: '' })
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)
  const on = (k) => (e) => setF({ ...f, [k]: e.target.value })

  const submit = async (e) => {
    e.preventDefault()
    if (f.next !== f.confirm) return setMsg({ kind: 'error', text: 'The two new passwords do not match.' })
    setBusy(true); setMsg(null)
    try {
      await api.changePassword(f.current, f.next)
      setF({ current: '', next: '', confirm: '' })
      setMsg({ kind: 'info', text: 'Password changed. Your other sessions were signed out.' })
      await refresh()
    } catch (ex) { setMsg({ kind: 'error', text: ex.message }) } finally { setBusy(false) }
  }

  return (
    <form className={forced ? 'card auth-card' : 'card form'} onSubmit={submit}>
      <h3>{forced ? 'Choose a new password' : 'Change password'}</h3>
      {forced && <p className="sub">Hello {user.full_name}. An administrator set a temporary password for you, so you need to choose your own before you continue.</p>}
      {msg && <Banner kind={msg.kind}>{msg.text}</Banner>}
      <label>{forced ? 'Temporary password' : 'Current password'}<input type="password" value={f.current} onChange={on('current')} autoComplete="current-password" required /></label>
      <label>New password<input type="password" value={f.next} onChange={on('next')} autoComplete="new-password" required />
        <span className="sub">At least 10 characters, not containing your username.</span></label>
      <label>Confirm new password<input type="password" value={f.confirm} onChange={on('confirm')} autoComplete="new-password" required /></label>
      <button className="primary" disabled={busy || !f.current || !f.next}>{busy ? 'Saving…' : 'Change password'}</button>
    </form>
  )
}

/* Shown instead of the app while the signed-in user still has a temporary password. */
export function ForcedPasswordChange() {
  const { logout } = useAuth()
  return (
    <div className="auth-wrap">
      <div>
        <div className="auth-logo center"><IconLogo width={34} height={34} /><div><h1>PIVC</h1><span>Video Analysis</span></div></div>
        <ChangePasswordForm forced />
        <p className="sub center"><button className="link" onClick={logout}>Sign out</button></p>
      </div>
    </div>
  )
}

export default function Account() {
  const { user } = useAuth()
  return (
    <>
      <header className="page-head"><div><h2>My account</h2><p>Your profile and password.</p></div></header>
      <section className="card">
        <h3>Profile</h3>
        <dl>
          <div><dt>Name</dt><dd>{user.full_name}</dd></div>
          <div><dt>Username</dt><dd>{user.username}</dd></div>
          <div><dt>Role</dt><dd>{ROLE_LABEL[user.role]}</dd></div>
        </dl>
        <p className="sub">{ROLE_HELP[user.role]} Ask an administrator if you need a different role.</p>
      </section>
      <ChangePasswordForm />
    </>
  )
}
