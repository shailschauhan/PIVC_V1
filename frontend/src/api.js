// All requests are same-origin, so the HttpOnly session cookie is sent automatically.
async function req(path, opts = {}) {
  const res = await fetch(`/api${path}`, { credentials: 'same-origin', ...opts })
  if (!res.ok) {
    let msg = res.statusText
    try { const j = await res.json(); msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail) } catch { /* ignore */ }
    // 401 from these just means "not signed in": it is the normal answer, not an expired session
    const quiet = ['/auth/login', '/auth/setup', '/auth/me', '/auth/status'].some((p) => path.startsWith(p))
    if (res.status === 401 && !quiet) window.dispatchEvent(new Event('auth:expired'))            // session ended while using the app
    if (res.status === 403 && /change your password/i.test(msg)) window.dispatchEvent(new Event('auth:refresh'))
    throw new Error(msg)
  }
  return res.json()
}

const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
const post = (path) => req(path, { method: 'POST' })
// ?a=1&a=2 for arrays; empty values are left out
export const query = (params) => {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) for (const x of Array.isArray(v) ? v : [v]) if (x !== '' && x != null) q.append(k, x)
  return q.toString()
}

export const api = {
  // sign-in and accounts
  authStatus: () => req('/auth/status'),
  me: () => req('/auth/me'),
  login: (username, password) => req('/auth/login', json('POST', { username, password })),
  setup: (b) => req('/auth/setup', json('POST', b)),
  logout: () => post('/auth/logout'),
  changePassword: (current_password, new_password) => req('/auth/change-password', json('POST', { current_password, new_password })),
  // user management (admin)
  users: () => req('/users'),
  createUser: (b) => req('/users', json('POST', b)),
  updateUser: (id, b) => req(`/users/${id}`, json('PUT', b)),
  resetPassword: (id, password) => req(`/users/${id}/reset-password`, json('POST', { password })),
  unlockUser: (id) => post(`/users/${id}/unlock`),
  deleteUser: (id) => req(`/users/${id}`, { method: 'DELETE' }),
  // videos
  dashboard: (days) => req(`/dashboard?days=${days}`),
  cases: (params) => req(`/cases?${new URLSearchParams(Object.entries(params).filter(([, v]) => v))}`),
  case: (id) => req(`/cases/${id}`),
  analyze: (id, tests) => (tests?.length ? req(`/cases/${id}/analyze`, json('POST', { tests })) : post(`/cases/${id}/analyze`)),
  review: (id, decision, notes) => req(`/cases/${id}/review`, json('POST', { decision, notes })),
  bulk: (action, ids, tests) => req('/cases/bulk', json('POST', { action, ids, ...(tests?.length ? { tests } : {}) })),
  upload: (form) => req('/cases/upload', { method: 'POST', body: form }),
  // configuration (admin)
  connectionTypes: () => req('/settings/connection-types'),
  connections: (type) => req(`/settings/connections?type=${type}`),
  createConnection: (b) => req('/settings/connections', json('POST', b)),
  updateConnection: (id, b) => req(`/settings/connections/${id}`, json('PUT', b)),
  activateConnection: (id) => post(`/settings/connections/${id}/activate`),
  deactivateConnection: (id) => post(`/settings/connections/${id}/deactivate`),
  deleteConnection: (id) => req(`/settings/connections/${id}`, { method: 'DELETE' }),
  testConnection: (b) => req('/settings/connections/test', json('POST', b)),
  // rules and versions (admin)
  rules: () => req('/rules'),
  saveRules: (changes, note) => req('/rules', json('PUT', { changes, note })),
  rulesHistory: () => req('/rules/history'),
  previewRules: (changes, days) => req(`/rules/preview?days=${days}`, json('POST', changes)),
  restoreRules: (version) => post(`/rules/${version}/restore`),
  // audit log (reviewer)
  audit: (params) => req(`/audit?${query(params)}`),
  auditFacets: () => req('/audit/facets'),
  // AI interpretation
  aiStatus: () => req('/ai/status'),
  interpret: (id) => post(`/cases/${id}/interpretation`),
  // history of test runs
  history: (params) => req(`/history?${query(params)}`),
  // reports (reviewer)
  reportOptions: () => req('/reports/options'),
  reportPreview: (params) => req(`/reports/preview?${query(params)}`),
  // engine and service health (admin)
  engines: () => req('/engines'),
  checkEngines: () => post('/engines/check'),
  engine: (key, hours) => req(`/engines/${key}?hours=${hours}`),
}
