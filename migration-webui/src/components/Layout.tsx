import React, { useEffect, useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import {
  Box,
  Drawer,
  AppBar,
  Toolbar,
  Typography,
  IconButton,
  Badge,
  Avatar,
  Tooltip,
  LinearProgress,
  useMediaQuery,
  useTheme,
  CssBaseline,
  Divider,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
} from '@mui/material'
import {
  Dashboard as DashboardIcon,
  People as PeopleIcon,
  Settings as SettingsIcon,
  BarChart as BarChartIcon,
  CheckCircle as VerifyIcon,
  Assessment as ReportIcon,
  ListAlt as ActivityIcon,
  Menu as MenuIcon,
  Notifications as NotificationsIcon,
  DarkMode as DarkModeIcon,
  LightMode as LightModeIcon,
  RocketLaunch as DeployIcon,
  Cloud as DriveIconNav,
  ErrorOutline as ErrorsIconNav,
  HelpOutline as HelpIconNav,
  School as WizardIconNav,
  Science as SeedWizardIconNav,
  Reorder as StagesIconNav,
  Dns as HostIcon,
  Shield as CommandIcon,
  Hub as MissionIcon,
  StopCircle as InterruptIcon,
  AdminPanelSettings as AdminIconNav,
} from '@mui/icons-material'
import { useMigrationStore } from '@/store'
import { fetchConfig, fetchJob, ConfigPayload, HostInfo, JobStatus, stopJob } from '@/api/client'
import { fetchMe, logout, Account } from '@/api/controlPlane'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import Logout from '@mui/icons-material/Logout'

// DriveMigration/ErrorHandling/HelpSystem existed as files with no route and
// no nav entry -- reachable by typing a URL nobody would guess, effectively
// unshipped. Added here alongside the App.tsx routes that now serve them.
const NAV_ITEMS = [
  { path: '/wizard', label: 'Setup Wizard', icon: <WizardIconNav /> },
  { path: '/seed-wizard', label: 'Seed Wizard', icon: <SeedWizardIconNav /> },
  { path: '/mission-control', label: 'Mission Control', icon: <MissionIcon /> },
  { path: '/dashboard', label: 'Overview (legacy)', icon: <DashboardIcon /> },
  { path: '/command', label: 'Command Center (legacy)', icon: <CommandIcon /> },
  { path: '/users', label: 'Users', icon: <PeopleIcon /> },
  { path: '/drive', label: 'Drive Migration', icon: <DriveIconNav /> },
  { path: '/activity', label: 'Activity', icon: <ActivityIcon /> },
  { path: '/system-health', label: 'System Health', icon: <BarChartIcon /> },
  { path: '/verification', label: 'Verification', icon: <VerifyIcon /> },
  { path: '/errors', label: 'Failures', icon: <ErrorsIconNav /> },
  { path: '/report', label: 'Final Report', icon: <ReportIcon /> },
  { path: '/help', label: 'Help', icon: <HelpIconNav /> },
  { path: '/settings', label: 'Deploy', icon: <DeployIcon /> },
]

const formatEta = (seconds: number): string => {
  if (seconds < 60) return `${seconds}s`
  const m = Math.round(seconds / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

interface LayoutProps {
  children: React.ReactNode
}

const Layout: React.FC<LayoutProps> = ({ children }) => {
  const theme = useTheme()
  const navigate = useNavigate()
  const location = useLocation()
  const { sidebarOpen, darkMode, toggleSidebar, toggleDarkMode, metrics, users } = useMigrationStore()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [host, setHost] = useState<HostInfo | null>(null)
  const [cfg, setCfg] = useState<ConfigPayload['config'] | null>(null)
  const [job, setJob] = useState<JobStatus | null>(null)
  const isDesktop = useMediaQuery('(min-width:960px)')
  const [account, setAccount] = useState<Account | null>(null)
  // Superadmin-only entry appended, never part of the static list -- a
  // regular client (every account but the one Aryan promotes on the VPS)
  // must not even see this exists. The backend refuses the underlying
  // calls regardless (require_superadmin), but there is no reason to
  // advertise a page that will just 403.
  const navItems = account?.is_superadmin
    ? [...NAV_ITEMS, { path: '/admin/accounts', label: 'Accounts (admin)', icon: <AdminIconNav /> }]
    : NAV_ITEMS
  const [notifAnchor, setNotifAnchor] = useState<null | HTMLElement>(null)
  const [profileAnchor, setProfileAnchor] = useState<null | HTMLElement>(null)

  // Fetched once: App.tsx already proved a session exists before Layout
  // ever mounts (that's the whole point of its own fetchMe() gate), so
  // this call is just for the name/email/plan to show, not a second
  // authorization check.
  useEffect(() => {
    fetchMe().then(setAccount).catch(() => setAccount(null))
  }, [])

  const handleSignOut = () => {
    setProfileAnchor(null)
    // Hard navigation, not react-router's navigate() -- same reason as
    // Login.tsx/Signup.tsx's fix: App.tsx's own `account` state is only
    // fetched once on mount, so a client-side route change here would
    // leave it stale and still truthy, and /login's own route guard
    // (`account ? <Navigate to="/mission-control"/> : <Login/>`) would
    // bounce straight back into the app the session that just ended.
    // /app prefix required -- see Login.tsx's comment: this is a raw
    // browser navigation, not routed through react-router's basename.
    logout().finally(() => { window.location.href = '/app/login' })
  }

  // Fetched once, not polled: a process's own hostname/code path/pid never
  // change while it is running (see webui.py's host_info(), which caches
  // this server-side for the same reason). This exists because a local
  // seed run and a deployed VPS instance can both bind 127.0.0.1:8080 and
  // look identical in the browser -- nothing on screen said which one a
  // given tab was actually talking to until now.
  useEffect(() => {
    fetchConfig().then((c) => {
      setHost(c.host)
      setCfg(c.config)
    }).catch(() => {})
  }, [])

  // Polled everywhere, not just on the page that started it -- webui.py's
  // Job is one global background process, so a seed (or migrate) kicked
  // off from the Seed/Setup Wizard must still show here while you are
  // looking at Dashboard or Settings.
  useEffect(() => {
    const poll = () => fetchJob(1_000_000_000).then(setJob).catch(() => {})
    poll()
    const id = setInterval(poll, 3000)
    return () => clearInterval(id)
  }, [])

  const handleDrawerToggle = () => {
    if (isDesktop) {
      toggleSidebar()
    } else {
      setMobileOpen(!mobileOpen)
    }
  }

  const memoryPct = metrics?.ram?.percentage ?? 0
  const ledgerProgress = users.length > 0
    ? Math.round(users.reduce((sum, u) => sum + u.progress, 0) / users.length)
    : 0
  // While a job is actually running, its own real progress is more useful
  // than the ledger average -- a seed run in particular does not touch
  // migration.db at all. progressPct/etaSeconds only ever come from a
  // real source (seed's own printed lines, or the migrate/delta/discover
  // ledger fraction -- see webui.py's _job_progress()); null means no
  // baseline exists yet, shown as an indeterminate bar rather than a
  // fabricated number.
  const jobRunning = job?.running ?? false
  const progressPct = jobRunning && job!.progressPct !== null ? job!.progressPct : ledgerProgress
  const progressIndeterminate = jobRunning && job!.progressPct === null

  const handleInterrupt = () => {
    if (!confirm(`Interrupt the running job (${job?.name})?`)) return
    stopJob()
  }

  const drawerContent = (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Box sx={{ px: 2, pb: 2, pt: 1, display: 'flex', alignItems: 'center', gap: 1.5 }}>
        <Box sx={{
          width: 40, height: 40, borderRadius: 2, bgcolor: 'primary.main',
          opacity: 0.1, position: 'absolute',
        }} />
        <Box sx={{
          width: 40, height: 40, borderRadius: 2, bgcolor: 'action.hover',
          display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
        }}>
          <DeployIcon sx={{ color: 'primary.main', fontSize: 22 }} />
        </Box>
        {sidebarOpen && (
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="h5" noWrap sx={{ color: 'primary.main', fontWeight: 700, lineHeight: 1.2 }}>
              Bitport
            </Typography>
            <Typography variant="caption" color="text.secondary" noWrap sx={{ display: 'block' }}>
              Source: {cfg?.source_domain || 'not set'}
            </Typography>
          </Box>
        )}
      </Box>
      <Divider />
      <List sx={{ pt: 1, px: 1, flex: 1, overflowY: 'auto' }}>
        {navItems.map((item) => {
          const isActive = location.pathname === item.path
          return (
            <ListItemButton
              key={item.path}
              onClick={() => {
                navigate(item.path)
                if (!isDesktop) setMobileOpen(false)
              }}
              sx={{
                mb: 0.25,
                // primary (blue), not secondary (green) -- green is
                // Google's success/status color, not its nav-active color;
                // every real Google product (Gmail, Drive, Admin Console)
                // highlights the active nav item in its brand blue.
                bgcolor: isActive ? 'primary.light' : 'transparent',
                color: isActive ? 'primary.dark' : 'text.secondary',
                fontWeight: isActive ? 700 : 400,
                '&:hover': { bgcolor: isActive ? 'primary.light' : 'action.hover' },
              }}
            >
              <ListItemIcon sx={{ color: 'inherit', minWidth: 40 }}>
                {item.icon}
              </ListItemIcon>
              {sidebarOpen && (
                <ListItemText primary={item.label} primaryTypographyProps={{ fontSize: 14, fontWeight: 'inherit' }} />
              )}
            </ListItemButton>
          )
        })}
      </List>
      <Box sx={{ p: 1, borderTop: '1px solid', borderColor: 'divider' }}>
        <ListItemButton onClick={toggleDarkMode}>
          <ListItemIcon sx={{ minWidth: 40 }}>{darkMode ? <LightModeIcon /> : <DarkModeIcon />}</ListItemIcon>
          {sidebarOpen && <ListItemText primary={darkMode ? 'Light Mode' : 'Dark Mode'} primaryTypographyProps={{ fontSize: 13 }} />}
        </ListItemButton>
      </Box>
    </Box>
  )

  return (
    <Box sx={{ display: 'flex' }}>
      <CssBaseline />
      <AppBar
        position="fixed"
        elevation={0}
        sx={{
          zIndex: (t) => t.zIndex.drawer + 1,
          bgcolor: 'background.paper',
          borderBottom: '1px solid',
          borderColor: 'divider',
          color: 'text.primary',
        }}
      >
        <Toolbar sx={{ gap: 1.5, flexWrap: 'wrap', py: 0.5 }}>
          <IconButton edge="start" color="inherit" onClick={handleDrawerToggle}>
            <MenuIcon />
          </IconButton>
          <Typography variant="h6" noWrap component="div" sx={{ fontWeight: 700, mr: 1 }}>
            Bitport
          </Typography>

          {/* Live job indicator: same running/name/progress/eta/elapsed the
             inline UI's header dot shows, so both surfaces read the same
             live state at a glance. */}
          {jobRunning && (
            <Box sx={{
              display: { xs: 'none', md: 'flex' }, alignItems: 'center', gap: 1.25,
              bgcolor: 'primary.light', border: '1px solid', borderColor: 'primary.main',
              borderRadius: 999, px: 2, py: 0.75,
            }}>
              <Box sx={{ position: 'relative', width: 10, height: 10 }}>
                <Box sx={{
                  position: 'absolute', inset: 0, borderRadius: '50%', bgcolor: 'primary.main',
                  opacity: 0.6, animation: 'ping 1.5s cubic-bezier(0,0,0.2,1) infinite',
                  '@keyframes ping': { '75%,100%': { transform: 'scale(2)', opacity: 0 } },
                }} />
                <Box sx={{ position: 'absolute', inset: 0, borderRadius: '50%', bgcolor: 'primary.main' }} />
              </Box>
              <Typography variant="caption" sx={{ fontWeight: 700, color: 'primary.dark' }} noWrap>
                {job!.name}
              </Typography>
              {!progressIndeterminate && (
                <>
                  <Divider orientation="vertical" flexItem sx={{ borderColor: 'primary.main', opacity: 0.4 }} />
                  <Typography variant="caption" sx={{ fontWeight: 700, color: 'primary.dark' }}>{progressPct}%</Typography>
                </>
              )}
              <Divider orientation="vertical" flexItem sx={{ borderColor: 'primary.main', opacity: 0.4 }} />
              <Typography variant="caption" color="text.secondary">{job!.elapsed}s elapsed</Typography>
              {job!.etaSeconds != null && (
                <>
                  <Divider orientation="vertical" flexItem sx={{ borderColor: 'primary.main', opacity: 0.4 }} />
                  <Typography variant="caption" color="text.secondary">ETA {formatEta(job!.etaSeconds)}</Typography>
                </>
              )}
            </Box>
          )}

          <Box sx={{ flexGrow: 1 }} />

          {/* Where this tab is actually talking to -- a local seed run and
             a deployed VPS instance can both bind 127.0.0.1:8080 and look
             identical in the browser otherwise. */}
          <Tooltip title={host ? (
            <>
              <div>Code: {host.code_path}</div>
              <div>PID: {host.pid}{host.commit ? ` · commit ${host.commit}` : ' · no git history (deployed copy)'}</div>
            </>
          ) : ''}>
            <Box sx={{
              display: { xs: 'none', lg: 'flex' }, alignItems: 'center', gap: 0.5,
              px: 1, py: 0.5, borderRadius: 1, border: '1px solid', borderColor: 'divider',
              bgcolor: 'background.default', cursor: 'default',
            }}>
              <HostIcon sx={{ fontSize: 16, color: 'text.secondary' }} />
              <Typography variant="caption" noWrap sx={{ color: 'text.secondary' }}>
                {host ? host.location : 'locating…'}
              </Typography>
            </Box>
          </Tooltip>

          <Box sx={{ display: { xs: 'none', md: 'flex' }, alignItems: 'center', gap: 2, mx: 1 }}>
            <Box sx={{ width: 90 }}>
              <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
                <Typography variant="caption" color="text.secondary">Memory</Typography>
                <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: 'tabular-nums' }}>{memoryPct}%</Typography>
              </Box>
              <LinearProgress
                variant="determinate" value={memoryPct}
                color={memoryPct >= 85 ? 'error' : memoryPct >= 65 ? 'warning' : 'primary'}
                sx={{ height: 4 }}
              />
            </Box>
          </Box>

          {jobRunning && (
            <Tooltip title="Interrupt the running job">
              <IconButton color="error" onClick={handleInterrupt}>
                <InterruptIcon />
              </IconButton>
            </Tooltip>
          )}
          {(() => {
            const items: string[] = []
            if (jobRunning) items.push(`${job!.name} is running (${progressIndeterminate ? 'in progress' : `${progressPct}%`})`)
            if (memoryPct >= 85) items.push(`Memory at ${memoryPct}% — approaching the limit`)
            return (
              <>
                <Badge badgeContent={items.length} color="error" invisible={items.length === 0}>
                  <IconButton color="inherit" onClick={(e) => setNotifAnchor(e.currentTarget)}>
                    <NotificationsIcon />
                  </IconButton>
                </Badge>
                <Menu
                  anchorEl={notifAnchor}
                  open={Boolean(notifAnchor)}
                  onClose={() => setNotifAnchor(null)}
                  anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
                  transformOrigin={{ vertical: 'top', horizontal: 'right' }}
                >
                  <Box sx={{ px: 2, py: 1, minWidth: 260 }}>
                    <Typography variant="body2" sx={{ fontWeight: 700 }}>Notifications</Typography>
                  </Box>
                  <Divider />
                  {items.length === 0 ? (
                    <MenuItem disabled>Nothing to report</MenuItem>
                  ) : items.map((text) => (
                    <MenuItem key={text} onClick={() => setNotifAnchor(null)} sx={{ whiteSpace: 'normal', maxWidth: 300 }}>
                      {text}
                    </MenuItem>
                  ))}
                </Menu>
              </>
            )
          })()}
          <Tooltip title={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}>
            <IconButton onClick={toggleDarkMode} color="inherit">
              {darkMode ? <LightModeIcon /> : <DarkModeIcon />}
            </IconButton>
          </Tooltip>
          <Tooltip title={account ? `${account.name} (${account.email})` : 'Loading…'}>
            <IconButton onClick={(e) => setProfileAnchor(e.currentTarget)} sx={{ p: 0.25 }}>
              <Avatar sx={{ width: 32, height: 32, bgcolor: 'primary.main', fontWeight: 700 }}>
                {(account?.name || '?').charAt(0).toUpperCase()}
              </Avatar>
            </IconButton>
          </Tooltip>
          <Menu
            anchorEl={profileAnchor}
            open={Boolean(profileAnchor)}
            onClose={() => setProfileAnchor(null)}
            anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
            transformOrigin={{ vertical: 'top', horizontal: 'right' }}
          >
            <Box sx={{ px: 2, py: 1.25, minWidth: 200 }}>
              <Typography variant="body2" sx={{ fontWeight: 700 }}>{account?.name || 'Loading…'}</Typography>
              <Typography variant="caption" color="text.secondary">{account?.email}</Typography>
            </Box>
            <Divider />
            <MenuItem onClick={() => { setProfileAnchor(null); navigate('/settings') }}>
              <ListItemIcon><SettingsIcon fontSize="small" /></ListItemIcon>
              Settings
            </MenuItem>
            <MenuItem onClick={() => { setProfileAnchor(null); navigate('/help') }}>
              <ListItemIcon><HelpIconNav fontSize="small" /></ListItemIcon>
              Help &amp; documentation
            </MenuItem>
            <Divider />
            <MenuItem onClick={handleSignOut} sx={{ color: 'error.main' }}>
              <ListItemIcon><Logout fontSize="small" color="error" /></ListItemIcon>
              Sign out
            </MenuItem>
          </Menu>
        </Toolbar>
      </AppBar>
      <Box component="nav" sx={{ width: isDesktop ? (sidebarOpen ? 260 : 72) : 0, flexShrink: { md: 0 } }}>
        {isDesktop ? (
          <Drawer
            variant="permanent"
            open={sidebarOpen}
            sx={{
              width: sidebarOpen ? 260 : 72,
              flexShrink: 0,
              '& .MuiDrawer-paper': {
                width: sidebarOpen ? 260 : 72,
                boxSizing: 'border-box',
                borderRight: '1px solid',
                borderColor: 'divider',
                bgcolor: 'background.paper',
                transition: theme.transitions.create('width', { easing: theme.transitions.easing.sharp, duration: 200 }),
              },
            }}
          >
            {drawerContent}
          </Drawer>
        ) : (
          <Drawer
            variant="temporary"
            open={mobileOpen}
            onClose={handleDrawerToggle}
            ModalProps={{ keepMounted: true }}
            sx={{
              '& .MuiDrawer-paper': { width: 260, boxSizing: 'border-box' },
            }}
          >
            {drawerContent}
          </Drawer>
        )}
      </Box>
      <Box
        component="main"
        sx={{
          flexGrow: 1,
          p: { xs: 1.5, sm: 3 },
          pt: { xs: 9, sm: 10 },
          // Nothing on this page should ever cause the page itself to
          // scroll sideways -- a wide table/log is its own overflow
          // container (TableContainer, or an explicit overflowX box), not
          // this one. Guarding it here catches anything that slips through.
          overflowX: 'hidden',
          maxWidth: '100vw',
          transition: theme.transitions.create('margin', { easing: theme.transitions.easing.sharp, duration: 200 }),
          minHeight: '100vh',
          bgcolor: 'background.default',
        }}
      >
        {children}
      </Box>
    </Box>
  )
}

export default Layout
