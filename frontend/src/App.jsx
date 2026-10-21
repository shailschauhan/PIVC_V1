import { Fragment, useEffect, useState } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import Dashboard from './pages/Dashboard.jsx'
import Cases from './pages/Cases.jsx'
import CaseDetail from './pages/CaseDetail.jsx'
import Upload from './pages/Upload.jsx'
import Configuration from './pages/Configuration.jsx'
import Users from './pages/Users.jsx'
import Audit from './pages/Audit.jsx'
import Reports from './pages/Reports.jsx'
import Rules from './pages/Rules.jsx'
import Health, { HealthDetail } from './pages/Health.jsx'
import History from './pages/History.jsx'
import Login from './pages/Login.jsx'
import Account, { ForcedPasswordChange } from './pages/Account.jsx'
import { ROLE_LABEL, useAuth } from './auth.jsx'
import { IconAccount, IconAudit, IconCollapse, IconConfig, IconDashboard, IconExpand, IconHealth, IconHistory, IconLogo, IconLogout, IconReports, IconReview, IconRules, IconUpload, IconUsers, IconVideos } from './icons.jsx'

// role = the lowest role that can use the page (the server enforces it; the menu just hides what you cannot use)
const NAV = [
  { to: '/', end: true, label: 'Dashboard', Icon: IconDashboard, role: 'viewer', section: 'Work' },
  { to: '/cases', label: 'All videos', Icon: IconVideos, role: 'viewer', section: 'Work' },
  { to: '/review', label: 'Review queue', Icon: IconReview, role: 'viewer', section: 'Work' },
  { to: '/upload', label: 'Upload videos', Icon: IconUpload, role: 'reviewer', section: 'Work' },
  { to: '/history', label: 'History', Icon: IconHistory, role: 'viewer', section: 'Work' },
  { to: '/reports', label: 'Reports', Icon: IconReports, role: 'reviewer', section: 'Records' },
  { to: '/audit', label: 'Audit log', Icon: IconAudit, role: 'reviewer', section: 'Records' },
  { to: '/rules', label: 'Rules & thresholds', Icon: IconRules, role: 'admin', section: 'Administration' },
  { to: '/health', label: 'Service health', Icon: IconHealth, role: 'admin', section: 'Administration' },
  { to: '/configuration', label: 'Configuration', Icon: IconConfig, role: 'admin', section: 'Administration' },
  { to: '/users', label: 'User management', Icon: IconUsers, role: 'admin', section: 'Administration' },
]

const KEY = 'sidebarCollapsed'
const read = () => { try { return localStorage.getItem(KEY) === '1' } catch { return false } }

function Guard({ role, children }) {
  const { can } = useAuth()
  return can(role) ? children : (
    <section className="card"><h3>No access</h3><p>Your role does not include this page. Ask an administrator if you need it.</p></section>
  )
}

function Shell() {
  const { user, can, logout } = useAuth()
  const [collapsed, setCollapsed] = useState(read)
  useEffect(() => { try { localStorage.setItem(KEY, collapsed ? '1' : '0') } catch { /* private mode: not remembered */ } }, [collapsed])
  const label = collapsed ? { 'aria-hidden': true } : {}

  return (
    <div className={`app ${collapsed ? 'collapsed' : ''}`}>
      <aside className="sidebar" id="sidebar">
        <div className="side-head">
          <div className="brand" title="PIVC Video Analysis">
            <IconLogo width={26} height={26} />
            <h1 className="nav-label">PIVC<span>Video Analysis</span></h1>
          </div>
          <button className="icon-btn" onClick={() => setCollapsed(!collapsed)} aria-expanded={!collapsed} aria-controls="sidebar"
            aria-label={collapsed ? 'Expand menu' : 'Collapse menu'} title={collapsed ? 'Expand menu' : 'Collapse menu'}>
            {collapsed ? <IconExpand /> : <IconCollapse />}
          </button>
        </div>
        <nav aria-label="Main">
          {NAV.filter((n) => can(n.role)).map(({ to, end, label: text, Icon, section }, i, all) => (
            <Fragment key={to}>
              {section !== all[i - 1]?.section && i > 0 && <div className="nav-section" role="presentation"><span className="nav-label" {...label}>{section}</span></div>}
              <NavLink to={to} end={end} title={collapsed ? text : undefined} aria-label={text}>
                <Icon /><span className="nav-label" {...label}>{text}</span>
              </NavLink>
            </Fragment>
          ))}
        </nav>
        <div className="side-foot">
          <NavLink to="/account" title={collapsed ? `${user.full_name} — ${ROLE_LABEL[user.role]}` : undefined} aria-label="My account" className="account-link">
            <IconAccount />
            <span className="nav-label who" {...label}><b>{user.full_name}</b><small>{ROLE_LABEL[user.role]}</small></span>
          </NavLink>
          <button className="nav-btn" onClick={logout} title={collapsed ? 'Sign out' : undefined} aria-label="Sign out">
            <IconLogout /><span className="nav-label" {...label}>Sign out</span>
          </button>
        </div>
      </aside>
      <main>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/cases" element={<Cases />} />
          <Route path="/review" element={<Cases queue />} />
          <Route path="/cases/:id" element={<CaseDetail />} />
          <Route path="/history" element={<History />} />
          <Route path="/upload" element={<Guard role="reviewer"><Upload /></Guard>} />
          <Route path="/reports" element={<Guard role="reviewer"><Reports /></Guard>} />
          <Route path="/audit" element={<Guard role="reviewer"><Audit /></Guard>} />
          <Route path="/rules" element={<Guard role="admin"><Rules /></Guard>} />
          <Route path="/health" element={<Guard role="admin"><Health /></Guard>} />
          <Route path="/health/:key" element={<Guard role="admin"><HealthDetail /></Guard>} />
          <Route path="/configuration" element={<Guard role="admin"><Configuration /></Guard>} />
          <Route path="/users" element={<Guard role="admin"><Users /></Guard>} />
          <Route path="/account" element={<Account />} />
        </Routes>
      </main>
    </div>
  )
}

export default function App() {
  const { loading, user } = useAuth()
  if (loading) return <div className="auth-wrap"><p className="sub">Loading…</p></div>
  if (!user) return <Login />
  if (user.must_change_password) return <ForcedPasswordChange />
  return <Shell />
}
