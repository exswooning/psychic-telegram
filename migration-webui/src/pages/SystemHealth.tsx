import React, { useEffect, useState } from 'react'
import {
  Box,
  Typography,
  Card,
  CardContent,
  Grid,
  LinearProgress,
  Chip,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Alert,
  AlertTitle,
  Avatar,
} from '@mui/material'
import {
  Memory as MemoryIcon,
  Speed as SpeedIcon,
  Storage as StorageIcon,
  NetworkCell as NetworkIcon,
  Psychology as WorkersIcon,
  CloudQueue as QueueIcon,
  Refresh as RefreshIcon,
  CheckCircle as HealthyIcon,
  Warning as WarningIcon,
  Error as ErrorIcon,
} from '@mui/icons-material'
import { useMigrationStore } from '@/store'
import { fetchFleet, FleetNode } from '@/api/controlPlane'

const SystemHealth: React.FC = () => {
  const { metrics } = useMigrationStore()
  const m = metrics

  // Same host, read the same way Mission Control's own Fleet node card
  // reads it (fleet_agent.py's ps scan) -- not webui.py's separate
  // load-average snapshot (metrics.cpu/ram/disk), which used to make this
  // page disagree with Mission Control on the exact same machine's CPU%,
  // purely from the two being sampled at different moments by different
  // processes. Falls back to the ledger-poller reading when no fleet node
  // is registered (fleet_agent.py is optional tooling, not everywhere).
  const [node, setNode] = useState<FleetNode | null>(null)
  useEffect(() => {
    const poll = () => fetchFleet().then((nodes) => setNode(nodes[0] ?? null)).catch(() => {})
    poll()
    const id = setInterval(poll, 5000)
    return () => clearInterval(id)
  }, [])

  const cpu = node?.cpu_pct ?? m.cpu
  const ramPct = node?.ram_pct ?? m.ram.percentage
  const diskPct = node?.disk_pct ?? m.disk.percentage

  const metricCards = [
    { title: 'CPU Usage', value: `${cpu}%`, icon: <SpeedIcon />, color: cpu > 80 ? 'error' : cpu > 60 ? 'warning' : 'success', progress: cpu },
    { title: 'RAM Usage', value: `${ramPct}%`, icon: <MemoryIcon />, color: ramPct > 85 ? 'error' : ramPct > 70 ? 'warning' : 'success', progress: ramPct, subtitle: `${m.ram.used} MB / ${m.ram.total} MB` },
    { title: 'Disk Usage', value: `${diskPct}%`, icon: <StorageIcon />, color: diskPct > 85 ? 'error' : diskPct > 70 ? 'warning' : 'success', progress: diskPct, subtitle: `${m.disk.used} GB / ${m.disk.total} GB` },
    { title: 'Network', value: `${m.network.down} MB/s`, icon: <NetworkIcon />, color: 'primary', progress: Math.min((m.network.down / 50) * 100, 100), subtitle: `↑ ${m.network.up} MB/s` },
    { title: 'API Health', value: m.apiHealth, icon: m.apiHealth === 'healthy' ? <HealthyIcon /> : <ErrorIcon />, color: m.apiHealth === 'healthy' ? 'success' : 'error', subtitle: 'Google API status' },
    { title: 'Google Quota', value: `${m.googleQuota.percentage}%`, icon: <QueueIcon />, color: m.googleQuota.percentage > 90 ? 'error' : m.googleQuota.percentage > 75 ? 'warning' : 'success', progress: m.googleQuota.percentage, subtitle: `${m.googleQuota.used}/${m.googleQuota.limit} requests` },
  ]

  return (
    <Box>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>System Health</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Real-time infrastructure monitoring</Typography>

      <Grid container spacing={2} sx={{ mb: 3 }}>
        {metricCards.map((card) => (
          <Grid item xs={12} sm={6} md={4} key={card.title}>
            <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
              <CardContent sx={{ p: 2.5 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1 }}>
                  <Typography variant="caption" color="text.secondary" sx={{ textTransform: 'uppercase' }}>{card.title}</Typography>
                  <Avatar sx={{ bgcolor: `${card.color}.light`, color: `${card.color}.contrastText`, width: 32, height: 32 }}>{card.icon}</Avatar>
                </Box>
                <Typography variant="h5" sx={{ fontWeight: 700, mb: 0.5 }}>{card.value}</Typography>
                {card.subtitle && <Typography variant="caption" color="text.secondary">{card.subtitle}</Typography>}
                <LinearProgress variant="determinate" value={card.progress} sx={{ height: 6, borderRadius: 3, mt: 1 }} />
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardContent sx={{ p: 3 }}>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>Worker Scaling</Typography>
          <Grid container spacing={2}>
            <Grid item xs={12} sm={4}>
              <Paper elevation={0} sx={{ p: 2, borderRadius: 2, bgcolor: 'background.paper' }}>
                <Typography variant="caption" color="text.secondary">Current Workers</Typography>
                <Typography variant="h4" sx={{ fontWeight: 700 }}>{m.workers.current}</Typography>
              </Paper>
            </Grid>
            <Grid item xs={12} sm={4}>
              <Paper elevation={0} sx={{ p: 2, borderRadius: 2, bgcolor: 'background.paper' }}>
                <Typography variant="caption" color="text.secondary">Max Workers</Typography>
                <Typography variant="h4" sx={{ fontWeight: 700 }}>{m.workers.max}</Typography>
              </Paper>
            </Grid>
            <Grid item xs={12}>
              <Alert severity="info" sx={{ borderRadius: 2 }}>
                <AlertTitle>Scaling Decision</AlertTitle>
                {m.workers.reason}
              </Alert>
            </Grid>
          </Grid>
        </CardContent>
      </Card>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <CardContent sx={{ p: 3 }}>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>Queue Status</Typography>
          <TableContainer>
            <Table>
              <TableHead>
                <TableRow>
                  <TableCell sx={{ fontWeight: 600 }}>Queue</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Pending</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Processing</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Completed</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Status</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                <TableRow>
                  <TableCell>Upload Queue</TableCell>
                  <TableCell>{m.uploadQueue}</TableCell>
                  <TableCell>4</TableCell>
                  <TableCell>{m.uploadQueue + 4}</TableCell>
                  <TableCell><Chip label="Active" size="small" color="primary" /></TableCell>
                </TableRow>
                <TableRow>
                  <TableCell>Retry Queue</TableCell>
                  <TableCell>{m.retryQueue}</TableCell>
                  <TableCell>0</TableCell>
                  <TableCell>0</TableCell>
                  <TableCell><Chip label={m.retryQueue > 0 ? 'Needs Attention' : 'Idle'} size="small" color={m.retryQueue > 0 ? 'warning' : 'default'} /></TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </TableContainer>
        </CardContent>
      </Card>
    </Box>
  )
}

export default SystemHealth