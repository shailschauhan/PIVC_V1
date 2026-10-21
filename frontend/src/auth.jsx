import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { api } from './api.js'

const Ctx = createContext(null)
export const useAuth = () => useContext(Ctx)

export const RANK = { viewer: 0, reviewer: 1, admin: 2 }
export const ROLE_LABEL = { viewer: 'Viewer', reviewer: 'Reviewer', admin: 'Administrator' }
export const ROLE_HELP = {
  viewer: 'Can look at the dashboard, videos and reports. Cannot change anything.',
  reviewer: 'Viewer, plus upload videos, record review decisions and re-run tests.',
  admin: 'Reviewer, plus delete videos, change thresholds and connections, and manage users.',
}

/* Who is signed in. The server enforces every permission; the UI only hides what a role cannot use. */
export function AuthProvider({ children }) {
  const [state, setState] = useState({ loading: true, user: null, setupRequired: false })

  const refresh = useCallback(async () => {
    try {
      const user = await api.me()
      setState({ loading: false, user, setupRequired: false })
    } catch {
      let setupRequired = false
      try { setupRequired = (await api.authStatus()).setup_required } catch { /* backend unreachable: show sign-in */ }
      setState({ loading: false, user: null, setupRequired })
    }
  }, [])

  useEffect(() => {
    refresh()
    // A request came back 401 while signed in: drop to the sign-in screen without another round trip (no loops).
    const expired = () => setState((s) => (s.user ? { loading: false, user: null, setupRequired: false } : s))
    window.addEventListener('auth:expired', expired)
    window.addEventListener('auth:refresh', refresh)   // e.g. the server says a password change is required
    return () => { window.removeEventListener('auth:expired', expired); window.removeEventListener('auth:refresh', refresh) }
  }, [refresh])

  const value = useMemo(() => ({
    ...state, refresh,
    can: (role) => !!state.user && RANK[state.user.role] >= RANK[role],
    login: async (u, p) => { const user = await api.login(u, p); setState({ loading: false, user, setupRequired: false }) },
    setup: async (b) => { const user = await api.setup(b); setState({ loading: false, user, setupRequired: false }) },
    logout: async () => { try { await api.logout() } finally { setState({ loading: false, user: null, setupRequired: false }) } },
  }), [state, refresh])

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
