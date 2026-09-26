import React from 'react'
import {
  Box,
  Typography,
  Card,
  CardContent,
  Grid,
  Chip,
  Stack,
  Avatar,
  Paper,
  Alert,
  AlertTitle,
} from '@mui/material'
import {
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
import RunReports from '@/components/RunReports'
import Incidents from '@/components/Incidents'

const FinalReport: React.FC = () => {
  const { report } = useMigrationStore()

  // /api/spa/report answers with a fully ZEROED object, never null, when no
  // migration has run -- so `!report` was never once true and the honest
  // fallback below was unreachable. What rendered instead was a green
  // "Migration Complete -- 0 of 0 users migrated successfully in --", which
  // is the worst possible reading of an empty ledger: it is the same screen
  // a real completed migration produces, and this project's ledger HAS been
  // reset (see reset_drive_ledger), which zeroes a finished run's numbers.
  // "Never ran" and "ran and moved nothing" must not look alike.
  if (!report || report.totalUsers === 0) {
    return (
      <Box>
        <Typography variant="h4" sx={{ fontWeight: 700, mb: 2 }}>Final Report</Typography>
        {/* Saved reports do not depend on anything running, so they are here
            even when the live summary below has nothing to say. */}
        <Incidents />
        <RunReports />
        <Alert severity="info" sx={{ mt: 2 }}>No live summary yet. Run a migration, then generate a report above.</Alert>
      </Box>
    )
  }

  // Finishing is not the same as succeeding. A run that moved nobody, or
  // lost users on the way, must not be announced in success green.
  const clean = report.failedUsers === 0 && report.successfulUsers === report.totalUsers

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
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Migration completion summary and downloadable reports</Typography>

      <Incidents />
      <RunReports />

      <Alert severity={clean ? 'success' : 'warning'} sx={{ mb: 3 }}>
        <AlertTitle>{clean ? 'Migration Complete' : 'Migration finished with failures'}</AlertTitle>
        {report.successfulUsers} of {report.totalUsers} users migrated
        successfully{report.totalDuration && report.totalDuration !== '\u2014'
          ? ` in ${report.totalDuration}`
          : ''}.
        {/* The duration comes from the job object, which a service restart
            clears -- so it is "\u2014" on any report read after one. An em
            dash is the honest unknown, but "migrated successfully in \u2014."
            reads as a broken sentence rather than a missing fact, so the
            clause is dropped instead of rendered empty. */}
        {report.failedUsers > 0 && ` ${report.failedUsers} failed.`}
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
          {report.totalDuration === '\u2014' && (
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              Timing is measured from the running job, which a service restart
              clears — so it is unavailable for a run that finished before the
              last restart. The item counts above come from the ledger and are
              unaffected.
            </Typography>
          )}
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
              {/* Not "verification": this is the share of users the ledger has
                  not marked FAILED, and nothing was compared against the
                  target to get it. Shown as a count, like the rest of the
                  tool; the checks that do compare the tenants are the
                  fidelity benchmarks in the report above. */}
              <Typography variant="caption" color="text.secondary">Users finished without a failure</Typography>
              <Typography variant="h5" sx={{ fontWeight: 700 }} data-testid="users-clean">
                {report.totalUsers - report.failedUsers} of {report.totalUsers}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                Taken from the ledger. Whether the target actually holds the data is what the fidelity
                benchmarks in a report check.
              </Typography>
            </Grid>
          </Grid>
        </CardContent>
      </Card>

    </Box>
  )
}

export default FinalReport