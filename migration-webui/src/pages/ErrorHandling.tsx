import React from 'react'
import {
  Box,
  Typography,
  Card,
  CardContent,
  CardHeader,
  Alert,
  AlertTitle,
  Button,
  Stack,
  Chip,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Avatar,
} from '@mui/material'
import {
  Error as ErrorIcon,
  Warning as WarningIcon,
  Refresh as RefreshIcon,
  Lightbulb as LightbulbIcon,
  Block as BlockIcon,
  Schedule as ScheduleIcon,
} from '@mui/icons-material'

const ErrorHandling: React.FC = () => {
  const errors = [
    {
      id: 1,
      type: 'Rate Limited',
      description: 'Google temporarily asked us to slow down.',
      explanation: 'The Google API returned a 429 (Too Many Requests) error. This is normal during large migrations — Google protects its servers from being overwhelmed.',
      resolution: 'The migration will automatically retry in 35 seconds. No action is required.',
      status: 'auto-retrying',
      autoFix: true,
    },
    {
      id: 2,
      type: 'Permission Denied',
      description: 'The source user does not have access to a shared drive.',
      explanation: 'The migration attempted to copy a file from a shared drive, but the source user\'s permissions do not include access to that drive.',
      resolution: 'Grant the source user access to the shared drive, or exclude the file from migration.',
      status: 'needs_attention',
      autoFix: false,
      actionRequired: 'Grant access in Admin Console > Drive > Sharing settings',
    },
    {
      id: 3,
      type: 'Quota Exceeded',
      description: 'The daily Google API quota has been reached.',
      explanation: 'Google imposes daily limits on API calls per project. When the quota is reached, new requests are rejected.',
      resolution: 'The migration will pause and resume automatically when the quota resets (typically after 24 hours).',
      status: 'paused',
      autoFix: false,
      actionRequired: 'Wait for quota reset, or request a quota increase in Google Cloud Console',
    },
    {
      id: 4,
      type: 'Network Timeout',
      description: 'The connection to Google Drive timed out.',
      explanation: 'A network interruption caused the API request to exceed the timeout threshold.',
      resolution: 'The migration will automatically retry the failed request.',
      status: 'retrying',
      autoFix: true,
    },
  ]

  return (
    <Box>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>Error Handling</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Friendly error cards with clear explanations and resolution steps</Typography>

      <Stack spacing={2} sx={{ mb: 3 }}>
        {errors.map((error) => (
          <Card key={error.id} elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent>
              <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 2 }}>
                <Avatar sx={{ bgcolor: error.status === 'auto-retrying' || error.status === 'retrying' ? 'warning.light' : error.status === 'paused' ? 'default' : 'error.light', color: error.status === 'paused' ? 'text.secondary' : 'error.contrastText', flexShrink: 0 }}>
                  {error.status === 'auto-retrying' || error.status === 'retrying' ? <ScheduleIcon /> : error.status === 'paused' ? <BlockIcon /> : <ErrorIcon />}
                </Avatar>
                <Box sx={{ flexGrow: 1 }}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5 }}>
                    <Typography variant="subtitle1" fontWeight={600}>{error.type}</Typography>
                    <Chip
                      label={error.status === 'auto-retrying' ? 'Auto-Retrying' : error.status === 'retrying' ? 'Retrying' : error.status === 'paused' ? 'Paused' : 'Needs Attention'}
                      size="small"
                      color={error.status === 'auto-retrying' || error.status === 'retrying' ? 'warning' : error.status === 'paused' ? 'default' : 'error'}
                      variant="outlined"
                    />
                  </Box>
                  <Typography variant="body2" sx={{ mb: 1 }}>{error.description}</Typography>
                  <Alert severity="info" sx={{ mb: 1, borderRadius: 2 }}>
                    <AlertTitle>Why did this happen?</AlertTitle>
                    {error.explanation}
                  </Alert>
                  <Alert severity="success" sx={{ borderRadius: 2 }}>
                    <AlertTitle>How to fix it</AlertTitle>
                    {error.resolution}
                  </Alert>
                  {!error.autoFix && error.actionRequired && (
                    <Alert severity="warning" sx={{ borderRadius: 2, mt: 1 }}>
                      <AlertTitle>Action Required</AlertTitle>
                      {error.actionRequired}
                      <Button variant="contained" color="warning" size="small" sx={{ ml: 2 }}>Fix Now</Button>
                    </Alert>
                  )}
                </Box>
              </Box>
            </CardContent>
          </Card>
        ))}
      </Stack>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <CardContent>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>Error Summary</Typography>
          <TableContainer>
            <Table>
              <TableHead>
                <TableRow>
                  <TableCell>Error Type</TableCell>
                  <TableCell>Count</TableCell>
                  <TableCell>Auto-Fix</TableCell>
                  <TableCell>Status</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                <TableRow>
                  <TableCell>Rate Limited (429)</TableCell>
                  <TableCell>3</TableCell>
                  <TableCell><Chip label="Yes" size="small" color="success" variant="outlined" /></TableCell>
                  <TableCell><Chip label="Retrying" size="small" color="warning" variant="outlined" /></TableCell>
                </TableRow>
                <TableRow>
                  <TableCell>Permission Denied (403)</TableCell>
                  <TableCell>1</TableCell>
                  <TableCell><Chip label="No" size="small" variant="outlined" /></TableCell>
                  <TableCell><Chip label="Needs Attention" size="small" color="error" variant="outlined" /></TableCell>
                </TableRow>
                <TableRow>
                  <TableCell>Quota Exceeded (403)</TableCell>
                  <TableCell>1</TableCell>
                  <TableCell><Chip label="No" size="small" variant="outlined" /></TableCell>
                  <TableCell><Chip label="Paused" size="small" variant="outlined" /></TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </TableContainer>
        </CardContent>
      </Card>
    </Box>
  )
}

export default ErrorHandling