import React from 'react'
import {
  Box,
  Typography,
  Button,
  Card,
  CardContent,
  Grid,
  LinearProgress,
  Chip,
  Stack,
  Avatar,
  Paper,
  Alert,
  AlertTitle,
} from '@mui/material'
import {
  Download as DownloadIcon,
  PictureAsPdf as PdfIcon,
  TableChart as CsvIcon,
  Assessment as DetailedIcon,
  GetApp as ExportIcon,
  PlayArrow as DeltaIcon,
  CheckCircle as SuccessIcon,
  Error as ErrorIcon,
  People as PeopleIcon,
  Email as EmailIcon,
  CloudDone as DriveIcon,
  Event as CalendarIcon,
  Group as GroupIcon,
  Storage as StorageIcon,
} from '@mui/icons-material'
import { useMigrationStore } from '@/store'

const FinalReport: React.FC = () => {
  const { report } = useMigrationStore()

  if (!report) {
    return (
      <Box>
        <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>Final Report</Typography>
        <Alert severity="info" sx={{ mt: 2 }}>No report available yet. Run a migration to generate the final report.</Alert>
      </Box>
    )
  }

  const stats = [
    { label: 'Total Users', value: report.totalUsers, icon: <PeopleIcon />, color: 'primary' },
    { label: 'Successful', value: report.successfulUsers, icon: <SuccessIcon />, color: 'success' },
    { label: 'Failed', value: report.failedUsers, icon: <ErrorIcon />, color: 'error' },
    { label: 'Data Migrated', value: report.dataMigrated, icon: <StorageIcon />, color: 'secondary' },
    { label: 'Emails', value: report.emailsMigrated, icon: <EmailIcon />, color: 'info' },
    { label: 'Drive Files', value: report.driveFilesMigrated, icon: <DriveIcon />, color: 'primary' },
    { label: 'Calendar Events', value: report.calendarEvents, icon: <CalendarIcon />, color: 'success' },
    { label: 'Contacts', value: report.contacts, icon: <PeopleIcon />, color: 'secondary' },
    { label: 'Groups', value: report.groups, icon: <GroupIcon />, color: 'info' },
    { label: 'Shared Drives', value: report.sharedDrives, icon: <DriveIcon />, color: 'warning' },
  ]

  return (
    <Box>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>Final Report</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Migration completion summary and exports</Typography>

      <Alert severity="success" sx={{ mb: 3 }}>
        <AlertTitle>Migration Complete</AlertTitle>
        {report.successfulUsers} of {report.totalUsers} users migrated successfully in {report.totalDuration}.
      </Alert>

      <Grid container spacing={2} sx={{ mb: 3 }}>
        {stats.map((stat) => (
          <Grid item xs={6} sm={4} md={2} key={stat.label}>
            <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', textAlign: 'center' }}>
              <CardContent sx={{ p: 2 }}>
                <Avatar sx={{ mx: 'auto', mb: 1, bgcolor: `${stat.color}.light`, color: `${stat.color}.contrastText` }}>
                  {stat.icon}
                </Avatar>
                <Typography variant="h5" sx={{ fontWeight: 700 }}>{stat.value}</Typography>
                <Typography variant="caption" color="text.secondary">{stat.label}</Typography>
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardContent sx={{ p: 3 }}>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>Performance Summary</Typography>
          <Grid container spacing={3}>
            <Grid item xs={12} sm={4}>
              <Typography variant="caption" color="text.secondary">Total Duration</Typography>
              <Typography variant="h5" sx={{ fontWeight: 700 }}>{report.totalDuration}</Typography>
            </Grid>
            <Grid item xs={12} sm={4}>
              <Typography variant="caption" color="text.secondary">Average Throughput</Typography>
              <Typography variant="h5" sx={{ fontWeight: 700 }}>{report.averageThroughput}</Typography>
            </Grid>
            <Grid item xs={12} sm={4}>
              <Typography variant="caption" color="text.secondary">Average Speed</Typography>
              <Typography variant="h5" sx={{ fontWeight: 700 }}>{report.averageSpeed}</Typography>
            </Grid>
            <Grid item xs={12}>
              <Typography variant="caption" color="text.secondary">Verification Success Rate</Typography>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mt: 0.5 }}>
                <LinearProgress variant="determinate" value={report.verificationSuccessRate} sx={{ flexGrow: 1, height: 10, borderRadius: 5 }} />
                <Typography variant="h5" sx={{ fontWeight: 700, minWidth: 60 }}>{report.verificationSuccessRate}%</Typography>
              </Box>
            </Grid>
          </Grid>
        </CardContent>
      </Card>

      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
        <Button variant="contained" startIcon={<PdfIcon />} size="large" sx={{ borderRadius: 2 }}>
          Download PDF Report
        </Button>
        <Button variant="outlined" startIcon={<CsvIcon />} size="large" sx={{ borderRadius: 2 }}>
          Download CSV
        </Button>
        <Button variant="outlined" startIcon={<DetailedIcon />} size="large" sx={{ borderRadius: 2 }}>
          View Detailed Report
        </Button>
        <Button variant="outlined" startIcon={<ExportIcon />} size="large" sx={{ borderRadius: 2 }}>
          Export Logs
        </Button>
        <Button variant="contained" color="secondary" startIcon={<DeltaIcon />} size="large" sx={{ borderRadius: 2 }}>
          Start Delta Sync
        </Button>
      </Stack>
    </Box>
  )
}

export default FinalReport