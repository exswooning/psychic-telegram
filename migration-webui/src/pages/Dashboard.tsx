import React from 'react'
import {
  Grid,
  Card,
  CardContent,
  Typography,
  Box,
  LinearProgress,
  Chip,
  Avatar,
  Stack,
  Paper,
  useTheme,
  useMediaQuery,
} from '@mui/material'
import {
  TrendingUp as TrendingUpIcon,
  People as PeopleIcon,
  Email as EmailIcon,
  CloudDone as CloudDoneIcon,
  Warning as WarningIcon,
  Error as ErrorIcon,
  Speed as SpeedIcon,
  Schedule as ScheduleIcon,
  Memory as MemoryIcon,
  Refresh as RefreshIcon,
} from '@mui/icons-material'
import { useMigrationStore } from '@/store'
import { MigrationStatus } from '@/types'

const statusConfig: Record<MigrationStatus, { color: string; icon: React.ReactElement; label: string }> = {
  not_started: { color: 'default', icon: <CloudDoneIcon />, label: 'Not Started' },
  waiting: { color: 'warning', icon: <ScheduleIcon />, label: 'Waiting' },
  in_progress: { color: 'primary', icon: <TrendingUpIcon />, label: 'In Progress' },
  retrying: { color: 'warning', icon: <WarningIcon />, label: 'Retrying' },
  completed: { color: 'success', icon: <CloudDoneIcon />, label: 'Completed' },
  failed: { color: 'error', icon: <ErrorIcon />, label: 'Failed' },
  needs_attention: { color: 'error', icon: <ErrorIcon />, label: 'Needs Attention' },
  paused: { color: 'default', icon: <ScheduleIcon />, label: 'Paused' },
  verified: { color: 'success', icon: <CloudDoneIcon />, label: 'Verified' },
  pending: { color: 'default', icon: <ScheduleIcon />, label: 'Pending' },
  mismatch: { color: 'warning', icon: <WarningIcon />, label: 'Mismatch' },
}

const StatCard: React.FC<{ title: string; value: string | number; icon: React.ReactElement; color?: string; subtitle?: string }> = ({ title, value, icon, color = 'primary', subtitle }) => (
  <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
    <CardContent sx={{ p: 2.5 }}>
      <Box sx={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between' }}>
        <Box>
          <Typography variant="caption" color="text.secondary" sx={{ textTransform: 'uppercase', letterSpacing: 0.5 }}>{title}</Typography>
          <Typography variant="h3" sx={{ fontWeight: 700, mt: 0.5 }}>{value}</Typography>
          {subtitle && <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>{subtitle}</Typography>}
        </Box>
        <Avatar sx={{ bgcolor: `${color}.light`, color: `${color}.contrastText`, width: 44, height: 44 }}>{icon}</Avatar>
      </Box>
    </CardContent>
  </Card>
)

const Dashboard: React.FC = () => {
  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('md'))
  const { users, stages, metrics, activities, lastUpdate } = useMigrationStore()
  const inProgress = users.filter((u) => u.status === 'in_progress').length
  const completed = users.filter((u) => u.status === 'completed').length
  const needsAttention = users.filter((u) => u.status === 'needs_attention').length
  const overallProgress = users.length > 0 ? Math.round(users.reduce((sum, u) => sum + u.progress, 0) / users.length) : 0
  const totalEmails = users.reduce((sum, u) => sum + (u.details?.mailbox?.itemsCompleted ?? 0), 0)
  const totalDriveFiles = users.reduce((sum, u) => sum + (u.details?.drive?.itemsCompleted ?? 0), 0)
  const totalEvents = users.reduce((sum, u) => sum + (u.details?.calendar?.itemsCompleted ?? 0), 0)

  return (
    <Box>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>Dashboard</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Last updated: {new Date(lastUpdate).toLocaleTimeString()}
      </Typography>

      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} sm={6} md={3}>
          <StatCard title="Overall Progress" value={`${overallProgress}%`} icon={<TrendingUpIcon />} color="primary" subtitle={`${completed} of ${users.length} users complete`} />
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <StatCard title="In Progress" value={inProgress} icon={<TrendingUpIcon />} color="info" subtitle={`${needsAttention} needs attention`} />
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <StatCard title="Emails Migrated" value={totalEmails} icon={<EmailIcon />} color="success" subtitle="Gmail messages" />
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <StatCard title="Drive Files" value={totalDriveFiles} icon={<CloudDoneIcon />} color="secondary" subtitle={`${totalEvents} calendar events`} />
        </Grid>
      </Grid>

      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} md={8}>
          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent sx={{ p: 3 }}>
              <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>Migration Progress</Typography>
              <Box sx={{ mb: 2 }}>
                <Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 0.5 }}>
                  <Typography variant="body2" fontWeight={500}>Overall Migration</Typography>
                  <Typography variant="body2" color="text.secondary">{overallProgress}%</Typography>
                </Box>
                <LinearProgress variant="determinate" value={overallProgress} sx={{ height: 12, borderRadius: 6 }} />
              </Box>
              {users.map((user) => {
                const config = statusConfig[user.status]
                return (
                  <Box key={user.email} sx={{ mb: 2 }}>
                    <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 0.5 }}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                        <Avatar sx={{ width: 28, height: 28, bgcolor: `${config.color}.light`, color: `${config.color}.contrastText`, fontSize: 12 }}>
                          {user.name.charAt(0)}
                        </Avatar>
                        <Typography variant="body2" fontWeight={500}>{user.name}</Typography>
                      </Box>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                        <Chip label={config.label} size="small" color={config.color as any} variant="outlined" />
                        <Typography variant="caption" color="text.secondary">{user.progress}%</Typography>
                      </Box>
                    </Box>
                    <LinearProgress variant="determinate" value={user.progress} sx={{ height: 6, borderRadius: 3 }} />
                    <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5, display: 'block' }}>{user.currentOperation}</Typography>
                  </Box>
                )
              })}
            </CardContent>
          </Card>
        </Grid>
        <Grid item xs={12} md={4}>
          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent sx={{ p: 3 }}>
              <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>Migration Stages</Typography>
              {stages.map((stage) => {
                const config = statusConfig[stage.status]
                return (
                  <Box key={stage.id} sx={{ mb: 1.5 }}>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                      <Box sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: `${config.color}.main`, flexShrink: 0 }} />
                      <Typography variant="body2" fontWeight={500} sx={{ flexGrow: 1 }}>{stage.name}</Typography>
                      <Typography variant="caption" color="text.secondary">{stage.progress}%</Typography>
                    </Box>
                    {stage.status === 'in_progress' && (
                      <LinearProgress variant="determinate" value={stage.progress} sx={{ height: 4, borderRadius: 2, mt: 0.5, ml: 4 }} />
                    )}
                  </Box>
                )
              })}
            </CardContent>
          </Card>

          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mt: 2 }}>
            <CardContent sx={{ p: 3 }}>
              <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>System Health</Typography>
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <Typography variant="body2" color="text.secondary"><MemoryIcon sx={{ verticalAlign: 'middle', mr: 0.5 }} /> Memory</Typography>
                  <Typography variant="body2" fontWeight={500}>{metrics.ram.percentage}%</Typography>
                </Box>
                <LinearProgress variant="determinate" value={metrics.ram.percentage} sx={{ height: 6, borderRadius: 3 }} />
                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <Typography variant="body2" color="text.secondary">CPU</Typography>
                  <Typography variant="body2" fontWeight={500}>{metrics.cpu}%</Typography>
                </Box>
                <LinearProgress variant="determinate" value={metrics.cpu} sx={{ height: 6, borderRadius: 3 }} />
                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <Typography variant="body2" color="text.secondary">Workers</Typography>
                  <Chip label={`${metrics.workers.current}/${metrics.workers.max}`} size="small" color="primary" variant="outlined" />
                </Box>
                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <Typography variant="body2" color="text.secondary">API Health</Typography>
                  <Chip label={metrics.apiHealth} size="small" color={metrics.apiHealth === 'healthy' ? 'success' : 'error'} />
                </Box>
              </Box>
            </CardContent>
          </Card>
        </Grid>
      </Grid>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <CardContent sx={{ p: 3 }}>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>Recent Activity</Typography>
          {activities.slice(0, 5).map((activity) => {
            const config = statusConfig[activity.status]
            return (
              <Box key={activity.id} sx={{ display: 'flex', alignItems: 'flex-start', gap: 1.5, py: 1, borderBottom: '1px solid', borderColor: 'divider' }}>
                <Box sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: `${config.color}.main`, mt: 0.5, flexShrink: 0 }} />
                <Box sx={{ flexGrow: 1 }}>
                  <Typography variant="body2"><strong>{activity.user}</strong> — {activity.action}</Typography>
                  <Typography variant="caption" color="text.secondary">{activity.details}</Typography>
                </Box>
                <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: 'nowrap' }}>
                  {new Date(activity.timestamp).toLocaleTimeString()}
                </Typography>
              </Box>
            )
          })}
        </CardContent>
      </Card>
    </Box>
  )
}

export default Dashboard