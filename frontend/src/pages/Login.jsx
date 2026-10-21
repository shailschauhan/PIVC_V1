import { useState } from 'react'
import { Banner } from '../components.jsx'
import { useAuth } from '../auth.jsx'
import { IconLogo } from '../icons.jsx'

/* Sign-in, or — on a brand-new install with no users — creation of the first administrator. */
export default function Login() {
  const { setupRequired, login, setup } = useAuth()
  const [f, setF] = useState({ full_name: '', username: '', password: '', confirm: '' })
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const on = (k) => (e) => setF({ ...f, [k]: e.target.value })

  const submit = async (e) => {
    e.preventDefault()
    setErr('')
    if (setupRequired && f.password !== f.confirm) return setErr('The two passwords do not match.')
    setBusy(true)
    try {
      if (setupRequired) await setup({ full_name: f.full_name, username: f.username, password: f.password })
      else await login(f.username, f.password)
    } catch (ex) { setErr(ex.message); setBusy(false) }
  }

  return (
    <div className="auth-wrap">
      <form className="card auth-card" onSubmit={submit}>
        <div className="auth-logo"><IconLogo width={34} height={34} /><div><h1>PIVC</h1><span>Video Analysis</span></div></div>
        <h2>{setupRequired ? 'Create the administrator account' : 'Sign in'}</h2>
        {setupRequired && <p className="sub">This is the first start, so there are no users yet. This account will be able to manage everything, including other users.</p>}
        {err && <Banner kind="error">{err}</Banner>}
        {setupRequired && <label>Full name<input value={f.full_name} onChange={on('full_name')} autoComplete="name" /></label>}
        <label>Username<input value={f.username} onChange={on('username')} autoComplete="username" autoFocus required /></label>
        <label>Password<input type="password" value={f.password} onChange={on('password')} autoComplete={setupRequired ? 'new-password' : 'current-password'} required /></label>
        {setupRequired && (
          <>
            <label>Confirm password<input type="password" value={f.confirm} onChange={on('confirm')} autoComplete="new-password" required /></label>
            <p className="sub">At least 10 characters, not containing the username.</p>
          </>
        )}
        <button className="primary" disabled={busy || !f.username || !f.password}>{busy ? 'Please wait…' : setupRequired ? 'Create account and sign in' : 'Sign in'}</button>
      </form>
    </div>
  )
}
