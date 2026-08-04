import React from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  Box,
  Typography,
  Button,
  Card,
  CardContent,
  CardHeader,
  Grid,
  LinearProgress,
  Chip,
  Stack,
  Avatar,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Alert,
  AlertTitle,
  Tooltip,
} from '@mui/material'
import { ArrowBack as ArrowBackIcon, Refresh as RefreshIcon, PlayArrow as PlayIcon, Pause as PauseIcon, Error as ErrorIcon } from '@mui/icons-material'
import { useMigrationStore } from '@/store'
import { statusLabel, statusColor, statusIcon } from '@/utils/formatters'

const UserDetail: React.FC = () => {
  const { email } = useParams<{ email: string }>()
  const navigate = useNavigate()
  const { users } = useMigrationStore()
  const user = users.find((u) => u.email === email)

  if (!user) {
    return (
      <Box>
        <Button startIcon={<ArrowBackIcon />} onClick={() => navigate('/users')} sx={{ mb: 2 }} variant="outlined" size="small">Back to Users</Button>
        <Alert severity="error"><AlertTitle>User Not Found</AlertTitle>No user found for {email}</Alert>
      </Box>
    )
  }

  const config = { color: statusColor(user.status) as string, label: statusLabel(user.status) }

  const serviceEntries = user.details ? Object.entries(user.details) : []

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 3 }}>
        <Button startIcon={<ArrowBackIcon />} onClick={() => navigate('/users')} variant="outlined" size="small">
          Back to Users
        </Button>
      </Box>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardContent sx={{ p: 3 }}>
          <Grid container spacing={3} alignItems="center">
            <Grid item>
              <Avatar sx={{ width: 56, height: 56, bgcolor: `${config.color}.light`, color: `${config.color}.contrastText`, fontSize: 24 }}>
                {user.name.charAt(0)}
              </Avatar>
            </Grid>
            <Grid item xs>
              <Typography variant="h5" sx={{ fontWeight: 700 }}>{user.name}</Typography>
              <Typography variant="body2" color="text.secondary">{user.email}</Typography>
            </Grid>
            <Grid item>
              <Chip label={config.label} color={config.color as any} variant="filled" />
            </Grid>
            <Grid item>
              <Typography variant="h3" sx={{ fontWeight: 700 }}>{user.progress}%</Typography>
              <Typography variant="caption" color="text.secondary">Overall</Typography>
            </Grid>
          </Grid>
          <Box sx={{ mt: 2 }}>
            <Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 0.5 }}>
              <Typography variant="body2">{user.currentOperation}</Typography>
              <Typography variant="caption" color="text.secondary">ETA: {user.estimatedTimeRemaining}</Typography>
            </Box>
            <LinearProgress variant="determinate" value={user.progress} sx={{ height: 10, borderRadius: 5 }} />
          </Box>
          <Stack direction="row" spacing={2} sx={{ mt: 2 }}>
            <Chip label={`${user.retries} retries`} size="small" color={user.retries > 0 ? 'warning' : 'default'} variant="outlined" />
            <Chip label={`${user.warnings} warnings`} size="small" color={user.warnings > 0 ? 'warning' : 'default'} variant="outlined" />
            <Chip label={`${user.errors} errors`} size="small" color={user.errors > 0 ? 'error' : 'default'} variant="outlined" />
          </Stack>
        </CardContent>
      </Card>

      <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>Service Progress</Typography>
      <Grid container spacing={2}>
        {serviceEntries.map(([key, service]) => {
          const s = service as any
          const sConfig = { color: statusColor(s.status) as string, label: statusLabel(s.status) }
          return (
            <Grid item xs={12} sm={6} md={4} key={key}>
              <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', height: '100%' }}>
                <CardContent>
                  <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1 }}>
                    <Typography variant="subtitle2" fontWeight={600} sx={{ textTransform: 'capitalize' }}>{key}</Typography>
                    <Chip label={sConfig.label} size="small" color={sConfig.color as any} variant="outlined" />
                  </Box>
                  <LinearProgress variant="determinate" value={s.progress} sx={{ height: 8, borderRadius: 4, mb: 1 }} />
                  <Typography variant="caption" color="text.secondary">
                    {s.itemsCompleted}/{s.itemsTotal} items
                  </Typography>
                  {s.currentItem && (
                    <Typography variant="caption" display="block" sx={{ mt: 0.5 }}>
                      {s.currentItem}
                    </Typography>
                  )}
                  {s.speed && (
                    <Typography variant="caption" color="text.secondary">Speed: {s.speed}</Typography>
                  )}
                  {s.eta && (
                    <Typography variant="caption" color="text.secondary">ETA: {s.eta}</Typography>
                  )}
                </CardContent>
              </Card>
            </Grid>
          )
        })}
      </Grid>

      {user.errors > 0 && (
        <Alert severity="error" sx={{ mt: 3 }}>
          <AlertTitle>Errors Detected</AlertTitle>
          {user.errors} error(s) found for this user. Click "Retry" to attempt recovery.
          <Button variant="contained" color="error" size="small" sx={{ ml: 2 }} startIcon={<RefreshIcon />}>
            Retry
          </Button>
        </Alert>
      )}
    </Box>
  )
}

export default UserDetail