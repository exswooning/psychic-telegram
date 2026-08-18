import React, { useEffect, useState } from 'react'
import {
  Box,
  Typography,
  TextField,
  InputAdornment,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TablePagination,
  Chip,
  LinearProgress,
  Avatar,
  IconButton,
  Tooltip,
  Card,
  CardContent,
  Grid,
  Alert,
  Button,
} from '@mui/material'
import {
  Search as SearchIcon, Person as PersonIcon, ArrowForward as ArrowForwardIcon,
  Bolt as RunningIcon,
} from '@mui/icons-material'
import { useNavigate } from 'react-router-dom'
import { useMigrationStore } from '@/store'
import { statusLabel, statusColor } from '@/utils/formatters'
import { fetchFleet, FleetNode } from '@/api/controlPlane'

const Users: React.FC = () => {
  const navigate = useNavigate()
  const { users, error, lastUpdate } = useMigrationStore()
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const [rowsPerPage, setRowsPerPage] = useState(10)

  // The rows below already reflect the CURRENT ledger state live -- audit_log
  // gets new rows the moment an active migrate/delta job writes them, and
  // this page is already polled every 4s (useMigration.ts). What's missing
  // is knowing WHETHER one is actually running right now: a user sitting at
  // "in_progress" could be mid-run, or could be a stale leftover from a run
  // interrupted hours ago -- nothing on this page could tell those apart.
  // Same fleet_agent.py ps-scan detection Jobs.tsx/RunningNow.tsx already use.
  const [activeJob, setActiveJob] = useState<FleetNode | null>(null)
  useEffect(() => {
    const poll = () => fetchFleet()
      .then((nodes) => setActiveJob(nodes.find((n) => n.active_job && n.job_pid) ?? null))
      .catch(() => {})
    poll()
    const id = setInterval(poll, 5000)
    return () => clearInterval(id)
  }, [])

  const filtered = users.filter(
    (u) =>
      u.name.toLowerCase().includes(search.toLowerCase()) ||
      u.email.toLowerCase().includes(search.toLowerCase())
  )

  const handleRowClick = (email: string) => {
    navigate(`/users/${email}`)
  }

  return (
    <Box>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>Users</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Manage and monitor individual migration progress
      </Typography>

      {error && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          Last poll failed ({error}). Showing data from {lastUpdate
            ? new Date(lastUpdate).toLocaleTimeString() : 'the last successful refresh'}
          , not necessarily live.
        </Alert>
      )}

      {activeJob && (
        <Alert severity="info" icon={<RunningIcon fontSize="inherit" />} sx={{ mb: 2 }}
          action={
            <Button color="inherit" size="small" onClick={() => navigate('/mission-control')}>
              Open Mission Control
            </Button>
          }>
          <strong>{activeJob.active_job}</strong> is running right now (pid {activeJob.job_pid}).
          {(activeJob.active_job === 'migrate' || activeJob.active_job === 'delta')
            ? ' The rows below are updating live as it writes to the ledger, not a stale snapshot.'
            : ' It does not write to this ledger, so the rows below reflect the last real migrate/delta run, not this job.'}
        </Alert>
      )}

      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} sm={4}>
          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent sx={{ p: 2 }}>
              <Typography variant="caption" color="text.secondary">Total Users</Typography>
              <Typography variant="h4" sx={{ fontWeight: 700 }}>{users.length}</Typography>
            </CardContent>
          </Card>
        </Grid>
        <Grid item xs={12} sm={4}>
          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent sx={{ p: 2 }}>
              <Typography variant="caption" color="text.secondary">In Progress</Typography>
              <Typography variant="h4" sx={{ fontWeight: 700, color: 'primary.main' }}>
                {users.filter((u) => u.status === 'in_progress').length}
              </Typography>
            </CardContent>
          </Card>
        </Grid>
        <Grid item xs={12} sm={4}>
          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent sx={{ p: 2 }}>
              <Typography variant="caption" color="text.secondary">Completed</Typography>
              <Typography variant="h4" sx={{ fontWeight: 700, color: 'success.main' }}>
                {users.filter((u) => u.status === 'completed').length}
              </Typography>
            </CardContent>
          </Card>
        </Grid>
      </Grid>

      <Paper elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 2 }}>
        <TextField
          fullWidth
          variant="outlined"
          placeholder="Search users by name or email..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchIcon color="action" />
              </InputAdornment>
            ),
            sx: { borderRadius: 2 },
          }}
          sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }}
        />
      </Paper>

      <TableContainer component={Paper} elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <Table>
          <TableHead>
            <TableRow sx={{ bgcolor: 'background.paper' }}>
              <TableCell sx={{ fontWeight: 600 }}>User</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Status</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Progress</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Current Operation</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>ETA</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Retries</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Warnings</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Errors</TableCell>
              <TableCell />
            </TableRow>
          </TableHead>
          <TableBody>
            {filtered
              .slice(page * rowsPerPage, page * rowsPerPage + rowsPerPage)
              .map((user) => {
                const color = statusColor(user.status)
                return (
                  <TableRow
                    hover
                    key={user.email}
                    onClick={() => handleRowClick(user.email)}
                    sx={{ cursor: 'pointer' }}
                  >
                    <TableCell>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                        <Avatar sx={{ width: 32, height: 32, bgcolor: `${color}.light`, color: `${color}.contrastText`, fontSize: 14 }}>
                          {user.name.charAt(0)}
                        </Avatar>
                        <Box>
                          <Typography variant="body2" fontWeight={500}>{user.name}</Typography>
                          <Typography variant="caption" color="text.secondary">{user.email}</Typography>
                        </Box>
                      </Box>
                    </TableCell>
                    <TableCell>
                      <Chip label={statusLabel(user.status)} size="small" color={color as any} variant="outlined" />
                    </TableCell>
                    <TableCell sx={{ minWidth: 120 }}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                        <LinearProgress variant="determinate" value={user.progress} sx={{ flexGrow: 1, height: 6, borderRadius: 3 }} />
                        <Typography variant="caption" fontWeight={500} sx={{ minWidth: 35, textAlign: 'right' }}>{user.progress}%</Typography>
                      </Box>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2" noWrap sx={{ maxWidth: 200 }}>{user.currentOperation}</Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2">{user.estimatedTimeRemaining}</Typography>
                    </TableCell>
                    <TableCell>
                      {user.retries > 0 ? <Chip label={user.retries} size="small" color="warning" variant="outlined" /> : <Typography variant="body2" color="text.secondary">0</Typography>}
                    </TableCell>
                    <TableCell>
                      {user.warnings > 0 ? <Chip label={user.warnings} size="small" color="warning" variant="outlined" /> : <Typography variant="body2" color="text.secondary">0</Typography>}
                    </TableCell>
                    <TableCell>
                      {user.errors > 0 ? <Chip label={user.errors} size="small" color="error" variant="outlined" /> : <Typography variant="body2" color="text.secondary">0</Typography>}
                    </TableCell>
                    <TableCell>
                      <Tooltip title="View Details">
                        <IconButton size="small" onClick={(e) => { e.stopPropagation(); handleRowClick(user.email) }}>
                          <ArrowForwardIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    </TableCell>
                  </TableRow>
                )
              })}
          </TableBody>
        </Table>
        <TablePagination
          rowsPerPageOptions={[5, 10, 25]}
          component="div"
          count={filtered.length}
          rowsPerPage={rowsPerPage}
          page={page}
          onPageChange={(_, newPage) => setPage(newPage)}
          onRowsPerPageChange={(e) => { setRowsPerPage(parseInt(e.target.value, 10)); setPage(0) }}
        />
      </TableContainer>
    </Box>
  )
}

export default Users