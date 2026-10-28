import { ReactNode, useEffect, useState } from 'react'
import { Link as RouterLink, useLocation, useNavigate } from 'react-router-dom'
import { AppBar, Avatar, Badge, Box, Button, CssBaseline, Divider, Drawer, IconButton, List, ListItemButton, ListItemIcon, ListItemText, Menu, MenuItem, Toolbar, Tooltip, Typography, useMediaQuery } from '@mui/material'
import { Add, AppsOutlined, ChevronRight, DarkModeOutlined, HelpOutline, LightModeOutlined, Logout, Menu as MenuIcon, Notifications as NotificationsIcon, SettingsOutlined, StopCircleOutlined } from '@mui/icons-material'
import { useMigrationStore } from '@/store'
import BitportMark from './BitportMark'
import WorkspaceSearch from './WorkspaceSearch'
import MigrationSwitcher from './MigrationSwitcher'
import ViewingAccountSelect from './ViewingAccountSelect'
import { WorkspaceContext, WorkspaceState } from './WorkspaceContext'
import { workspaceFeatures } from '@/workspaceFeatures'
import { formatDuration } from '@/utils/formatters'
export interface WorkspaceNavItem { path: string; label: string; icon: ReactNode; group: string }
interface Props extends WorkspaceState { children: ReactNode; navItems: WorkspaceNavItem[]; onSignOut: () => void; onInterrupt: () => void; progress: number | null }
const primaryOrder = ['/overview', '/migrations', '/mirror', '/jobs', '/users', '/services', '/verification', '/one-to-one', '/activity']
export default function WorkspaceShell({ children, navItems, onSignOut, onInterrupt, progress, ...state }: Props) {
  const { account, job, host, owed } = state
  const { darkMode, toggleDarkMode, sidebarOpen, toggleSidebar } = useMigrationStore()
  const desktop = useMediaQuery('(min-width:960px)')
  const [mobileOpen, setMobileOpen] = useState(false)
  const [notifications, setNotifications] = useState<HTMLElement | null>(null)
  const [profile, setProfile] = useState<HTMLElement | null>(null)
  const location = useLocation(), navigate = useNavigate()
  useEffect(() => { if (window.scrollY > 0) window.scrollTo({ top: 0, behavior: 'auto' }) }, [location.pathname])
  const width = sidebarOpen || !desktop ? 248 : 72
  const title = workspaceFeatures.find(f => f.path === location.pathname)?.label || (location.pathname.startsWith('/migrations/') ? 'Migration details' : location.pathname.startsWith('/users/') ? 'Person details' : 'Workspace')
  const notices: { text: string; to?: string }[] = []
  if (job?.running) notices.push({ text: `${job.name} is running`, to: '/jobs' })
  for (const o of owed) {
    if (o.uncopyable) notices.push({ to: `/migrations/${o.accountId}`, text: `${o.targetDomain}: ${o.uncopyable.toLocaleString()} files could not be copied. Open the migration for details.` })
    if (o.shares) notices.push({ to: `/migrations/${o.accountId}`, text: `${o.targetDomain}: ${o.shares.toLocaleString()} shares waiting for ${o.colleagues.toLocaleString()} colleagues with no target account yet (${o.examples.join(', ')}${o.colleagues > o.examples.length ? ', …' : ''}). They are granted automatically once those colleagues are migrated.` })
  }
  const go = (path: string) => { navigate(path); setMobileOpen(false) }
  const toggle = () => desktop ? toggleSidebar() : setMobileOpen(v => !v)
  const active = (path: string) => location.pathname === path || (path === '/migrations' && location.pathname.startsWith('/migrations/'))
  const navigation = <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column', px: 1.5, pt: desktop ? 1 : 2 }}>
    <Tooltip title={sidebarOpen || !desktop ? '' : 'New migration'} placement="right"><Button onClick={() => go('/wizard')} startIcon={<Add />} variant="contained" sx={{ alignSelf: 'flex-start', mb: 2.5, ml: .5, minHeight: 56, minWidth: sidebarOpen || !desktop ? 174 : 44, px: sidebarOpen || !desktop ? 2.5 : 1, borderRadius: '16px', bgcolor: darkMode ? 'primary.light' : '#c2e7ff', color: darkMode ? 'text.primary' : '#001d35', '& .MuiButton-startIcon': { mr: sidebarOpen || !desktop ? 1.5 : 0 }, '&:hover': { bgcolor: 'primary.light' } }}>{(sidebarOpen || !desktop) && 'New migration'}</Button></Tooltip>
    <List aria-label="Workspace" sx={{ py: 0, flex: 1, minHeight: 0, overflowY: 'auto' }}>
      {primaryOrder.flatMap(path => { const item = navItems.find(i => i.path === path); if (!item) return []; return [<Tooltip key={path} title={sidebarOpen || !desktop ? '' : item.label} placement="right"><ListItemButton aria-label={item.label} aria-current={active(path) ? 'page' : undefined} onClick={() => go(path)} sx={{ minHeight: 44, mb: .25, px: 2, bgcolor: active(path) ? 'primary.light' : 'transparent', color: active(path) ? 'primary.dark' : 'text.secondary', '&:hover': { bgcolor: active(path) ? 'primary.light' : 'action.hover' } }}><ListItemIcon sx={{ minWidth: 36, color: 'inherit', '& svg': { fontSize: 21 } }}>{item.icon}</ListItemIcon>{(sidebarOpen || !desktop) && <ListItemText primary={item.label} primaryTypographyProps={{ fontSize: 14, fontWeight: active(path) ? 500 : 400 }} />}</ListItemButton></Tooltip>] })}

    </List>
    <Box sx={{ pt: 1.5, pb: 1 }}>
      <ListItemButton aria-label="All tools" onClick={() => go('/tools')} aria-current={active('/tools') ? 'page' : undefined} sx={{ minHeight: 44, px: 2, mt: 1, bgcolor: active('/tools') ? 'primary.light' : 'transparent' }}><ListItemIcon sx={{ minWidth: 36 }}><AppsOutlined fontSize="small" /></ListItemIcon>{(sidebarOpen || !desktop) && <ListItemText primary="All tools" primaryTypographyProps={{ fontSize: 14 }} />}</ListItemButton>
      <ListItemButton onClick={() => go('/settings')} aria-label="Settings" sx={{ px: 2 }}><ListItemIcon sx={{ minWidth: 36 }}><SettingsOutlined fontSize="small" /></ListItemIcon>{(sidebarOpen || !desktop) && <ListItemText primary="Settings" primaryTypographyProps={{ fontSize: 13 }} />}</ListItemButton>
      <ListItemButton onClick={() => go('/help')} aria-label="Help and feedback" sx={{ px: 2 }}><ListItemIcon sx={{ minWidth: 36 }}><HelpOutline fontSize="small" /></ListItemIcon>{(sidebarOpen || !desktop) && <ListItemText primary="Help & feedback" primaryTypographyProps={{ fontSize: 13 }} />}</ListItemButton>
      {(sidebarOpen || !desktop) && <Tooltip title={host ? `${host.hostname || ''} · ${host.commit || 'deployed copy'}` : 'Server information is loading'}><Typography variant="caption" color="text.secondary" sx={{ display: 'block', px: 2, mt: 1.5, fontSize: 11 }}>{host?.location || 'Connecting…'}</Typography></Tooltip>}
    </Box>
  </Box>
  return <WorkspaceContext.Provider value={state}>
    <Box sx={{ display: 'flex', bgcolor: 'background.default', minHeight: '100vh' }}><CssBaseline />
      <Box component="a" href="#workspace-content" sx={{ position: 'fixed', top: -80, left: 16, zIndex: 2000, p: 2, bgcolor: 'background.paper', '&:focus': { top: 8 } }}>Skip to content</Box>
      <AppBar position="fixed" elevation={0} sx={{ zIndex: t => t.zIndex.drawer + 1, bgcolor: 'background.default', color: 'text.primary' }}>
        <Toolbar sx={{ minHeight: { xs: '64px !important', md: '72px !important' }, gap: { xs: .5, md: 2 }, px: { xs: '12px !important', md: '16px !important' }, flexWrap: 'nowrap' }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0, width: { xs: 'auto', md: 228 } }}><IconButton aria-label="Toggle navigation" aria-controls="workspace-navigation" aria-expanded={desktop ? sidebarOpen : mobileOpen} onClick={toggle}><MenuIcon /></IconButton><Box component={RouterLink} to="/overview" sx={{ display: 'flex', gap: 1, alignItems: 'center', textDecoration: 'none', color: 'text.secondary' }}><BitportMark /><Typography sx={{ fontSize: { xs: 20, md: 24 }, fontWeight: 400, letterSpacing: '-.4px' }}>Bitport</Typography></Box></Box>
          <WorkspaceSearch operator={!!account?.is_superadmin} seed={!!account?.seed_enabled} />
          <Box sx={{ flex: 1 }} />
          {job?.running && <Button data-testid="running-job-pill" aria-label={`${job.name} running — open Jobs`} onClick={() => go('/jobs')} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go('/jobs') } }} sx={{ display: { xs: 'none', lg: 'inline-flex' }, bgcolor: 'primary.light', color: 'primary.dark', whiteSpace: 'nowrap', fontSize: 12, px: 1.5 }}><Box component="span" className="workspace-live-dot" sx={{ width: 6, height: 6, borderRadius: '50%', bgcolor: 'primary.main', mr: 1 }} />{job.name}{progress !== null ? ` · ${progress}%` : ''}<Box component="span" sx={{ ml: 1, color: 'text.secondary' }}>{formatDuration(job.elapsed * 1000)}</Box></Button>}
          <Badge badgeContent={notices.length} color="error"><IconButton aria-label="Notifications" onClick={e => setNotifications(e.currentTarget)}><NotificationsIcon /></IconButton></Badge>
          <IconButton aria-label={darkMode ? 'Switch to light mode' : 'Switch to dark mode'} onClick={toggleDarkMode} sx={{ display: { xs: 'none', sm: 'inline-flex' } }}>{darkMode ? <LightModeOutlined /> : <DarkModeOutlined />}</IconButton>
          <IconButton aria-label="Account menu" onClick={e => setProfile(e.currentTarget)} sx={{ p: .5 }}><Avatar sx={{ width: 34, height: 34, bgcolor: darkMode ? 'primary.light' : '#c2e7ff', color: 'text.primary', fontSize: 15 }}>{(account?.name || '?').charAt(0).toUpperCase()}</Avatar></IconButton>
        </Toolbar>
      </AppBar>
      <Menu anchorEl={notifications} open={!!notifications} onClose={() => setNotifications(null)}><Typography variant="subtitle1" sx={{ px: 2, py: 1 }}>Notifications</Typography><Divider />{notices.length ? notices.map((n, i) => <MenuItem key={i} data-testid={n.to?.startsWith('/migrations/') ? 'notif-owed' : undefined} onClick={() => { setNotifications(null); if (n.to) go(n.to) }} sx={{ maxWidth: 380, whiteSpace: 'normal', fontSize: 14 }}>{n.text}</MenuItem>) : <MenuItem disabled>Nothing to report</MenuItem>}{job?.running && !job.external && <MenuItem onClick={() => { setNotifications(null); onInterrupt() }} sx={{ color: 'error.main' }}><StopCircleOutlined fontSize="small" sx={{ mr: 1 }} />Interrupt the running job</MenuItem>}</Menu>
      <Menu anchorEl={profile} open={!!profile} onClose={() => setProfile(null)}><Box sx={{ px: 2, py: 1 }}><Typography variant="body2">{account?.name}</Typography><Typography variant="caption" color="text.secondary">{account?.email}</Typography></Box><Divider /><MenuItem onClick={() => { setProfile(null); go('/settings') }}>Settings</MenuItem><MenuItem onClick={() => { setProfile(null); toggleDarkMode() }}>{darkMode ? 'Light mode' : 'Dark mode'}</MenuItem><MenuItem component="a" href="/app/mission-control">Open current interface</MenuItem><Divider /><MenuItem onClick={() => { setProfile(null); onSignOut() }}><Logout fontSize="small" sx={{ mr: 1 }} />Sign out</MenuItem></Menu>
      <Box component="nav" sx={{ width: desktop ? width : 0, flexShrink: 0, transition: 'width 220ms cubic-bezier(.2,0,0,1)' }}>
        <Drawer id="workspace-navigation" variant={desktop ? 'permanent' : 'temporary'} open={desktop || mobileOpen} onClose={() => setMobileOpen(false)} ModalProps={{ keepMounted: true }} sx={{ '& .MuiDrawer-paper': { width, transition: 'width 220ms cubic-bezier(.2,0,0,1)', boxSizing: 'border-box', top: desktop ? 72 : 64, height: desktop ? 'calc(100% - 72px)' : 'calc(100% - 64px)', bgcolor: 'background.default', border: 0 } }}>{navigation}</Drawer>
      </Box>
      <Box component="main" id="workspace-content" tabIndex={-1} sx={{ flex: 1, minWidth: 0, bgcolor: 'background.paper', mt: { xs: 8, md: 9 }, mr: { xs: 1, md: 2 }, ml: { xs: 1, md: 1.5 }, mb: 2, borderRadius: { xs: '20px', md: '24px' }, p: { xs: 2, md: 3.5 }, overflowX: 'hidden' }}>
        <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 1.5, mb: 2.5 }}><Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flex: 1, minWidth: 150 }}><Typography component={RouterLink} to="/migrations" sx={{ textDecoration: 'none', color: 'text.secondary', fontSize: 12 }}>Migrations</Typography><ChevronRight sx={{ fontSize: 15, color: 'text.secondary' }} /><Typography variant="caption">{title}</Typography></Box><MigrationSwitcher /><ViewingAccountSelect /></Box>
        <Box key={location.pathname} data-workspace-page sx={{ maxWidth: 1560, mx: 'auto', minWidth: 0 }}>{children}</Box>
      </Box>
    </Box>
  </WorkspaceContext.Provider>
}
