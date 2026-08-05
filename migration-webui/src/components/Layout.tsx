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
  ListItem,
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
  CloudDone as CloudDoneIcon,
  Cloud as DriveIconNav,
  ErrorOutline as ErrorsIconNav,
  HelpOutline as HelpIconNav,
  School as WizardIconNav,
  Science as SeedWizardIconNav,
} from '@mui/icons-material'
import { useMigrationStore } from '@/store'
import { fetchConfig, fetchJob, HostInfo, JobStatus } from '@/api/client'
// DriveMigration/ErrorHandling/HelpSystem existed as files with no route and
// no nav entry -- reachable by typing a URL nobody would guess, effectively
// unshipped. Added here alongside the App.tsx routes that now serve them.
const NAV_ITEMS = [
  { path: '/wizard', label: 'Setup Wizard', icon: <WizardIconNav /> },
  { path: '/seed-wizard', label: 'Seed Wizard', icon: <SeedWizardIconNav /> },
  { path: '/dashboard', label: 'Dashboard', icon: <DashboardIcon /> },
  { path: '/users', label: 'Users', icon: <PeopleIcon /> },
  { path: '/drive', label: 'Drive Migration', icon: <DriveIconNav /> },
  { path: '/activity', label: 'Activity Feed', icon: <ActivityIcon /> },
  { path: '/system-health', label: 'System Health', icon: <BarChartIcon /> },
  { path: '/verification', label: 'Verification', icon: <VerifyIcon /> },
  { path: '/errors', label: 'Errors & Retries', icon: <ErrorsIconNav /> },
  { path: '/report', label: 'Final Report', icon: <ReportIcon /> },
  { path: '/help', label: 'Help', icon: <HelpIconNav /> },
  { path: '/settings', label: 'Settings', icon: <SettingsIcon /> },
]

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
  const [job, setJob] = useState<JobStatus | null>(null)
  const isDesktop = useMediaQuery('(min-width:960px)')
  const notifications = 3

  // Fetched once, not polled: a process's own hostname/code path/pid never
  // change while it is running (see webui.py's host_info(), which caches
  // this server-side for the same reason). This exists because a local
  // seed run and a deployed VPS instance can both bind 127.0.0.1:8080 and
  // look identical in the browser -- nothing on screen said which one a
  // given tab was actually talking to until now.
  useEffect(() => {
    fetchConfig().then((c) => setHost(c.host)).catch(() => {})
  }, [])

  // Polled everywhere, not just on the page that started it -- webui.py's
  // Job is one global background process, so a seed (or migrate) kicked
  // off from the Seed/Setup Wizard must still show here while you are
  // looking at Dashboard or Settings. `since` is set past any real log
  // length on purpose: this only needs name/running/rc/progressPct, not
  // the full transcript JobProgress already streams on its own page.
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
  // migration.db at all (seed_sandbox.py writes straight to Google's APIs;
  // see webui_spa.py's module docstring), so the ledger figure would just
  // sit frozen at whatever a previous migration left it at. progressPct is
  // only ever set for a seed job (see webui.py's _seed_progress_pct());
  // any other running job (migrate, discover, delta) has no parsed number
  // of its own, so it falls back to the same real ledger figure, which for
  // those jobs *is* live and accurate.
  const jobRunning = job?.running ?? false
  const progressPct = jobRunning && job!.progressPct !== null ? job!.progressPct : ledgerProgress
  const progressLabel = jobRunning ? `${job!.name}…` : 'Migration progress'
  const progressIndeterminate = jobRunning && job!.progressPct === null

  const drawerContent = (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Toolbar sx={{ display: 'flex', alignItems: 'center', px: 2 }}>
        <CloudDoneIcon sx={{ mr: 1, color: 'primary.main', fontSize: 28 }} />
        <Typography variant="h6" noWrap component="div" sx={{ fontWeight: 700 }}>
          Migration Tool
        </Typography>
      </Toolbar>
      <Divider />
      <List sx={{ pt: 1 }}>
        {NAV_ITEMS.map((item) => {
          const isActive = location.pathname === item.path
          return (
            <ListItem
              button
              key={item.path}
              onClick={() => {
                navigate(item.path)
                if (!isDesktop) setMobileOpen(false)
              }}
              sx={{
                mx: 1,
                borderRadius: 2,
                bgcolor: isActive ? 'primary.light' : 'transparent',
                color: isActive ? 'primary.contrastText' : 'text.primary',
                '&:hover': {
                  bgcolor: isActive ? 'primary.light' : 'action.hover',
                },
              }}
            >
              <ListItemIcon sx={{ color: isActive ? 'inherit' : 'text.secondary', minWidth: 40 }}>
                {item.icon}
              </ListItemIcon>
              <ListItemText primary={item.label} primaryTypographyProps={{ fontSize: 14, fontWeight: isActive ? 600 : 400 }} />
            </ListItem>
          )
        })}
      </List>
      <Box sx={{ mt: 'auto', p: 2 }}>
        <Divider />
        <ListItem button onClick={toggleDarkMode} sx={{ borderRadius: 2, mt: 1 }}>
          <ListItemIcon>{darkMode ? <LightModeIcon /> : <DarkModeIcon />}</ListItemIcon>
          <ListItemText primary={darkMode ? 'Light Mode' : 'Dark Mode'} primaryTypographyProps={{ fontSize: 13 }} />
        </ListItem>
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
        <Toolbar>
          <IconButton edge="start" color="inherit" onClick={handleDrawerToggle} sx={{ mr: 2 }}>
            <MenuIcon />
          </IconButton>
          <Typography variant="h6" noWrap component="div" sx={{ fontWeight: 600, color: 'text.primary' }}>
            Google Workspace Migration
          </Typography>
          <Box sx={{ flexGrow: 1 }} />

          {/* Where this tab is actually talking to, plus the two numbers
             that matter most while a run is going: is this host under
             memory pressure (resources.py throttles workers under it),
             and how far along the migration is. Found the need for this
             the hard way -- a local seed run and a deployed VPS instance
             can both bind 127.0.0.1:8080 and look identical in the
             browser. */}
          <Tooltip title={host ? (
            <>
              <div>Code: {host.code_path}</div>
              <div>PID: {host.pid}{host.commit ? ` · commit ${host.commit}` : ' · no git history (deployed copy)'}</div>
            </>
          ) : ''}>
            <Box sx={{
              display: 'flex', alignItems: 'center', gap: 0.75, mr: 2.5,
              px: 1, py: 0.5, borderRadius: 1, bgcolor: 'action.hover', cursor: 'default',
            }}>
              <Box sx={{
                width: 7, height: 7, borderRadius: '50%', flexShrink: 0,
                bgcolor: host && host.location !== 'Local machine' ? 'success.main' : 'text.disabled',
              }} />
              <Typography variant="caption" noWrap sx={{ fontFamily: 'ui-monospace, monospace', color: 'text.secondary' }}>
                {host ? host.location : 'locating…'}
              </Typography>
            </Box>
          </Tooltip>

          <Box sx={{ display: { xs: 'none', md: 'flex' }, alignItems: 'center', gap: 2, mr: 2.5 }}>
            <Box sx={{ width: 90 }}>
              <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
                <Typography variant="caption" color="text.secondary">Memory</Typography>
                <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: 'tabular-nums' }}>{memoryPct}%</Typography>
              </Box>
              <LinearProgress
                variant="determinate" value={memoryPct}
                color={memoryPct >= 85 ? 'error' : memoryPct >= 65 ? 'warning' : 'primary'}
                sx={{ height: 4, borderRadius: 2 }}
              />
            </Box>
            <Box sx={{ width: 110 }}>
              <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
                <Typography variant="caption" color="text.secondary" noWrap>{progressLabel}</Typography>
                {!progressIndeterminate && (
                  <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: 'tabular-nums' }}>{progressPct}%</Typography>
                )}
              </Box>
              {progressIndeterminate ? (
                <LinearProgress sx={{ height: 4, borderRadius: 2 }} />
              ) : (
                <LinearProgress variant="determinate" value={progressPct} sx={{ height: 4, borderRadius: 2 }} />
              )}
            </Box>
          </Box>

          <Badge badgeContent={notifications} color="error" sx={{ mr: 2 }}>
            <IconButton color="inherit">
              <NotificationsIcon />
            </IconButton>
          </Badge>
          <Tooltip title={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}>
            <IconButton onClick={toggleDarkMode} color="inherit">
              {darkMode ? <LightModeIcon /> : <DarkModeIcon />}
            </IconButton>
          </Tooltip>
          <Avatar sx={{ width: 32, height: 32, bgcolor: 'primary.main', ml: 1 }}>A</Avatar>
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
          p: 3,
          pt: 10,
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