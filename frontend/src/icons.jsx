/* Small stroke icons (24x24 grid). Decorative: the label next to them (or aria-label on the button) carries the meaning. */
const base = { width: 20, height: 20, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true }
const make = (paths) => function Icon(props) { return <svg {...base} {...props}>{paths}</svg> }

export const IconDashboard = make(<><rect x="3" y="3" width="7" height="9" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" /><rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="16" width="7" height="5" rx="1.5" /></>)
export const IconVideos = make(<><rect x="3" y="5" width="13" height="14" rx="2" /><path d="M16 10l5-3v10l-5-3z" /></>)
export const IconReview = make(<><rect x="5" y="4" width="14" height="17" rx="2" /><path d="M9 4V3h6v1" /><path d="M9 13l2 2 4-4" /></>)
export const IconUpload = make(<><path d="M12 16V4" /><path d="M7 9l5-5 5 5" /><path d="M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3" /></>)
export const IconConfig = make(<><path d="M4 7h10M18 7h2M4 17h2M10 17h10" /><circle cx="16" cy="7" r="2" /><circle cx="8" cy="17" r="2" /></>)
export const IconUsers = make(<><circle cx="9" cy="8" r="3.2" /><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6" /><path d="M16 5.2a3.2 3.2 0 010 6.1" /><path d="M18 14.3c2 .8 3.4 2.8 3.4 5.2" /></>)
export const IconAudit = make(<><path d="M7 3h8l4 4v14H7z" /><path d="M15 3v4h4" /><path d="M10 12h6M10 16h6" /></>)
export const IconReports = make(<><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></>)
export const IconRules = make(<><path d="M12 3v18M5 7l7-4 7 4" /><path d="M2.5 13L5 7l2.5 6a2.5 2.5 0 01-5 0zM16.5 13L19 7l2.5 6a2.5 2.5 0 01-5 0z" /></>)
export const IconHealth = make(<><path d="M3 12h4l2.5-6 4 12 2.5-6H21" /></>)
export const IconHistory = make(<><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>)
export const IconAccount = make(<><circle cx="12" cy="8" r="3.5" /><path d="M5 20c0-3.9 3.1-7 7-7s7 3.1 7 7" /></>)
export const IconLogout = make(<><path d="M9 4H6a2 2 0 00-2 2v12a2 2 0 002 2h3" /><path d="M16 8l4 4-4 4" /><path d="M20 12H9" /></>)
export const IconCollapse = make(<><path d="M15 6l-6 6 6 6" /></>)
export const IconExpand = make(<><path d="M9 6l6 6-6 6" /></>)
export const IconLogo = make(<><path d="M12 3l8 3v6c0 4.5-3.2 7.8-8 9-4.8-1.2-8-4.5-8-9V6z" /><path d="M9 12l2.2 2.2L15 10.5" /></>)
