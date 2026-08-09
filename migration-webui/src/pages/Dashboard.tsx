import React from 'react'
import {
  Box,
  Typography,
  LinearProgress,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tooltip,
} from '@mui/material'
import {
  AddToDrive as DriveIcon,
  Mail as MailIcon,
  CalendarMonth as CalendarIcon,
  Contacts as ContactsIcon,
  Chat as ChatIcon,
  VpnKey as PermissionsIcon,
  Inventory2 as ItemsIcon,
  Group as GroupIcon,
} from '@mui/icons-material'
import { useMigrationStore } from '@/store'
import { MigrationStatus, ServiceProgress } from '@/types'

type ServiceKey = 'drive' | 'mailbox' | 'calendar' | 'contacts' | 'chat' | 'permissions'

const SERVICES: { key: ServiceKey; label: string; icon: React.ReactElement }[] = [
  { key: 'drive', label: 'Drive', icon: <DriveIcon /> },
  { key: 'mailbox', label: 'Gmail', icon: <MailIcon /> },
  { key: 'calendar', label: 'Calendar', icon: <CalendarIcon /> },
  { key: 'contacts', label: 'Contacts', icon: <ContactsIcon /> },
  { key: 'chat', label: 'Chat', icon: <ChatIcon /> },
  { key: 'permissions', label: 'Permissions', icon: <PermissionsIcon /> },
]

const statusChipSx: Record<MigrationStatus, { bg: string; fg: string }> = {
  completed: { bg: '#e6f4ea', fg: '#137333' },
  verified: { bg: '#e6f4ea', fg: '#137333' },
  in_progress: { bg: '#e6f4ea', fg: '#137333' },
  retrying: { bg: '#fef7e0', fg: '#b06000' },
  waiting: { bg: '#fef7e0', fg: '#b06000' },
  mismatch: { bg: '#fef7e0', fg: '#b06000' },
  failed: { bg: '#fce8e6', fg: '#c5221f' },
  needs_attention: { bg: '#fce8e6', fg: '#c5221f' },
  not_started: { bg: '#f1f0f4', fg: '#414754' },
  pending: { bg: '#f1f0f4', fg: '#414754' },
  paused: { bg: '#f1f0f4', fg: '#414754' },
}

/** One service's real completion, summed across every user's own
 * ServiceProgress rather than re-derived -- itemsCompleted/itemsTotal here
 * are the same numbers Users.tsx and UserDetail.tsx already show per user. */
function aggregate(services: (ServiceProgress | undefined)[]): {
  done: number; total: number; pct: number; status: MigrationStatus
} {
  const present = services.filter((s): s is ServiceProgress => !!s)
  const done = present.reduce((sum, s) => sum + s.itemsCompleted, 0)
  const total = present.reduce((sum, s) => sum + s.itemsTotal, 0)
  const pct = total > 0 ? Math.round((done / total) * 100) : 0
  const anyFailed = present.some((s) => s.status === 'failed' || s.status === 'needs_attention')
  const anyRetrying = present.some((s) => s.status === 'retrying' || s.status === 'mismatch')
  const allDone = present.length > 0 && present.every((s) => s.status === 'completed' || s.status === 'verified')
  const anyStarted = present.some((s) => s.status !== 'not_started' && s.status !== 'pending')
  const status: MigrationStatus = anyFailed ? 'needs_attention'
    : allDone ? 'completed'
    : anyRetrying ? 'retrying'
    : anyStarted ? 'in_progress'
    : 'not_started'
  return { done, total, pct, status }
}

const serviceMiniBars = (statuses: MigrationStatus[]) => (
  <Box sx={{ display: 'flex', gap: 0.5, justifyContent: 'center' }}>
    {statuses.map((s, i) => (
      <Box key={i} sx={{
        height: 16, width: 6, borderRadius: 0.5,
        bgcolor: s === 'completed' || s === 'verified' ? 'success.main'
          : s === 'failed' || s === 'needs_attention' ? 'error.main'
          : s === 'in_progress' ? 'primary.main'
          : 'divider',
      }} />
    ))}
  </Box>
)

const Dashboard: React.FC = () => {
  const { users } = useMigrationStore()
  const overallProgress = users.length > 0 ? Math.round(users.reduce((sum, u) => sum + u.progress, 0) / users.length) : 0
  const usersDone = users.filter((u) => u.status === 'completed' || u.status === 'verified').length
  const itemsDone = users.reduce((sum, u) => sum + (u.details ? Object.values(u.details).reduce((s, sv) => s + sv.itemsCompleted, 0) : 0), 0)
  const itemsTotal = users.reduce((sum, u) => sum + (u.details ? Object.values(u.details).reduce((s, sv) => s + sv.itemsTotal, 0) : 0), 0)

  const active = users
    .filter((u) => u.status === 'in_progress' || u.status === 'retrying' || u.status === 'needs_attention')
    .slice(0, 12)

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
      <Box>
        <Typography variant="h2" sx={{ mb: 0.5 }}>Overview</Typography>
        <Typography variant="body2" color="text.secondary">Live migration status across every service and user.</Typography>
      </Box>

      {/* Total Migration Progress strip -- the one number everything else
         on this page is relative to. */}
      <Box sx={{ bgcolor: 'background.paper', border: '1px solid', borderColor: 'divider', borderRadius: 3, p: 3 }}>
        <Box sx={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', alignItems: 'flex-end', gap: 1, mb: 1.5 }}>
          <Typography variant="h6">Total Migration Progress</Typography>
          <Box sx={{ display: 'flex', gap: 3, flexWrap: 'wrap' }}>
            <Typography variant="body2" color="text.secondary" sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
              <ItemsIcon sx={{ fontSize: 16 }} /> {itemsDone.toLocaleString()} / {itemsTotal.toLocaleString()} items
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
              <GroupIcon sx={{ fontSize: 16 }} /> {usersDone} / {users.length} users
            </Typography>
          </Box>
        </Box>
        <Box sx={{ position: 'relative' }}>
          <LinearProgress variant="determinate" value={overallProgress} sx={{ height: 16 }} />
          <Typography variant="caption" sx={{
            position: 'absolute', right: 8, top: '50%', transform: 'translateY(-50%)',
            color: overallProgress > 8 ? '#fff' : 'text.secondary', fontWeight: 700,
          }}>
            {overallProgress}%
          </Typography>
        </Box>
      </Box>

      <Box sx={{ display: 'flex', flexDirection: { xs: 'column', lg: 'row' }, gap: 3 }}>
        {/* Service Status bento grid -- aggregated from every user's own
           ServiceProgress, the same source Users.tsx/UserDetail.tsx use. */}
        <Box sx={{ flex: 1 }}>
          <Typography variant="overline" color="text.secondary" sx={{ mb: 1, display: 'block' }}>
            Service Status
          </Typography>
          <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' }, gap: 2 }}>
            {SERVICES.map(({ key, label, icon }) => {
              const agg = aggregate(users.map((u) => u.details?.[key]))
              const sx = statusChipSx[agg.status]
              return (
                <Box key={key} sx={{
                  bgcolor: 'background.paper', border: '1px solid', borderColor: 'divider',
                  borderRadius: 2, p: 2,
                }}>
                  <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', mb: 2 }}>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                      <Box sx={{
                        width: 36, height: 36, borderRadius: 1.5, bgcolor: 'action.hover',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        color: 'text.secondary',
                      }}>
                        {icon}
                      </Box>
                      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>{label}</Typography>
                    </Box>
                    <Box sx={{
                      px: 1.25, py: 0.25, borderRadius: 999, bgcolor: sx.bg, color: sx.fg,
                      fontSize: 11, fontWeight: 700, textTransform: 'uppercase',
                    }}>
                      {agg.status.replace('_', ' ')}
                    </Box>
                  </Box>
                  <Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 0.5 }}>
                    <Typography variant="caption" color="text.secondary">Items migrated</Typography>
                    <Typography variant="caption" sx={{ fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>
                      {agg.done.toLocaleString()} / {agg.total.toLocaleString()}
                    </Typography>
                  </Box>
                  <LinearProgress variant="determinate" value={agg.pct} sx={{ height: 6 }} />
                </Box>
              )
            })}
          </Box>
        </Box>

        {/* Currently Active Users -- only the ones actually in flight, not
           the full roster (Users.tsx is the place for that). */}
        <Box sx={{ width: { xs: '100%', lg: 420 }, flexShrink: 0 }}>
          <Box sx={{ bgcolor: 'background.paper', border: '1px solid', borderColor: 'divider', borderRadius: 2, overflow: 'hidden' }}>
            <Box sx={{ p: 2, borderBottom: '1px solid', borderColor: 'divider', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <Box>
                <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>Currently Active Users</Typography>
                <Typography variant="caption" color="text.secondary">Live streaming migration status</Typography>
              </Box>
              <Box sx={{ position: 'relative', width: 8, height: 8 }}>
                <Box sx={{
                  position: 'absolute', inset: 0, borderRadius: '50%', bgcolor: 'primary.main',
                  opacity: 0.6, animation: 'ping 1.5s cubic-bezier(0,0,0.2,1) infinite',
                  '@keyframes ping': { '75%,100%': { transform: 'scale(2)', opacity: 0 } },
                }} />
                <Box sx={{ position: 'absolute', inset: 0, borderRadius: '50%', bgcolor: 'primary.main' }} />
              </Box>
            </Box>
            <TableContainer sx={{ maxHeight: 520 }}>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    <TableCell sx={{ fontWeight: 600 }}>User</TableCell>
                    <TableCell align="center" sx={{ fontWeight: 600, width: 70 }}>Services</TableCell>
                    <TableCell align="right" sx={{ fontWeight: 600, width: 60 }}>Status</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {active.map((u) => {
                    const statuses = SERVICES.map(({ key }) => u.details?.[key]?.status ?? 'not_started')
                    const isError = u.status === 'needs_attention'
                    return (
                      <TableRow key={u.email} sx={{ bgcolor: isError ? 'error.light' : 'transparent', '& .MuiTableCell-root': { borderColor: 'divider' } }}>
                        <TableCell>
                          <Typography variant="body2" sx={{ fontWeight: 600 }} noWrap>{u.email}</Typography>
                          <Typography variant="caption" color="text.secondary" noWrap sx={{ display: 'block' }}>
                            {u.currentOperation || '—'}
                          </Typography>
                        </TableCell>
                        <TableCell align="center">
                          <Tooltip title={SERVICES.map((s, i) => `${s.label}: ${statuses[i]}`).join(' · ')}>
                            {serviceMiniBars(statuses)}
                          </Tooltip>
                        </TableCell>
                        <TableCell align="right">
                          {isError ? (
                            <Typography variant="caption" color="error.main" sx={{ fontWeight: 700 }}>Err</Typography>
                          ) : (
                            <Box sx={{
                              display: 'inline-block', px: 1, py: 0.25, borderRadius: 999,
                              bgcolor: 'primary.light', color: 'primary.dark', fontWeight: 700, fontSize: 12,
                            }}>
                              {u.progress}%
                            </Box>
                          )}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                  {active.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={3}>
                        <Typography variant="body2" color="text.secondary" sx={{ py: 3, textAlign: 'center' }}>
                          No users actively migrating right now.
                        </Typography>
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </TableContainer>
          </Box>
        </Box>
      </Box>
    </Box>
  )
}

export default Dashboard
