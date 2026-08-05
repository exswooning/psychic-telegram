import React, { useState } from 'react'
import {
  Box,
  Typography,
  Paper,
  List,
  ListItem,
  ListItemIcon,
  ListItemText,
  ListItemSecondaryAction,
  Chip,
  Divider,
  TextField,
  InputAdornment,
  Badge,
  IconButton,
  Tooltip,
} from '@mui/material'
import {
  Search as SearchIcon,
  FilterList as FilterIcon,
  Info as InfoIcon,
  CheckCircle as CompletedIcon,
  Warning as WarningIcon,
  Error as ErrorIcon,
  HourglassEmpty as WaitingIcon,
  Refresh as RetryingIcon,
  CloudDone as InProgressIcon,
  Refresh as RefreshIcon,
} from '@mui/icons-material'
import { useMigrationStore } from '@/store'
import { statusLabel, statusColor } from '@/utils/formatters'

const ActivityFeed: React.FC = () => {
  const { activities } = useMigrationStore()
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<string>('all')

  const filtered = activities.filter((a) => {
    const matchesSearch = a.user.toLowerCase().includes(search.toLowerCase()) ||
      a.action.toLowerCase().includes(search.toLowerCase()) ||
      a.details?.toLowerCase().includes(search.toLowerCase())
    const matchesFilter = filter === 'all' || a.status === filter
    return matchesSearch && matchesFilter
  })

  const statusIcon = (status: string) => {
    switch (status) {
      case 'completed': return <CompletedIcon color="success" />
      case 'in_progress': return <InProgressIcon color="primary" />
      case 'waiting': return <WaitingIcon color="action" />
      case 'retrying': return <RetryingIcon color="warning" />
      case 'needs_attention': return <ErrorIcon color="error" />
      default: return <InfoIcon color="action" />
    }
  }

  return (
    <Box>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>Activity Feed</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Real-time human-readable migration events</Typography>

      <Paper elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', p: 2, mb: 2, display: 'flex', gap: 2, flexWrap: 'wrap', alignItems: 'center' }}>
        <TextField
          placeholder="Search activities..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          InputProps={{
            startAdornment: <InputAdornment position="start"><SearchIcon color="action" /></InputAdornment>,
            size: 'small',
          }}
          sx={{ minWidth: 200 }}
        />
        <TextField
          select
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          size="small"
          sx={{ minWidth: 150 }}
        >
          <option value="all">All Statuses</option>
          <option value="completed">Completed</option>
          <option value="in_progress">In Progress</option>
          <option value="waiting">Waiting</option>
          <option value="retrying">Retrying</option>
          <option value="needs_attention">Needs Attention</option>
        </TextField>
        <Tooltip title="Refresh">
          <IconButton><RefreshIcon /></IconButton>
        </Tooltip>
      </Paper>

      <Paper elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <List>
          {filtered.map((activity, index) => (
            <React.Fragment key={activity.id}>
              <ListItem>
                <ListItemIcon>{statusIcon(activity.status)}</ListItemIcon>
                <ListItemText
                  primary={
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                      <Typography variant="body2" component="span"><strong>{activity.user}</strong></Typography>
                      <Typography variant="body2" component="span" color="text.secondary">— {activity.action}</Typography>
                    </Box>
                  }
                  secondary={
                    <Box>
                      <Typography variant="caption" color="text.secondary">{activity.details}</Typography>
                      <Typography variant="caption" color="text.secondary" display="block">{new Date(activity.timestamp).toLocaleString()}</Typography>
                    </Box>
                  }
                />
                <ListItemSecondaryAction>
                  <Chip label={statusLabel(activity.status)} size="small" color={statusColor(activity.status) as any} variant="outlined" />
                </ListItemSecondaryAction>
              </ListItem>
              {index < filtered.length - 1 && <Divider variant="inset" component="li" />}
            </React.Fragment>
          ))}
        </List>
        {filtered.length === 0 && (
          <Box sx={{ p: 3, textAlign: 'center' }}>
            <Typography variant="body2" color="text.secondary">No activities match your filters</Typography>
          </Box>
        )}
      </Paper>
    </Box>
  )
}

export default ActivityFeed