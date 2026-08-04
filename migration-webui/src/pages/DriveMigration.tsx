import React from 'react'
import {
  Box,
  Typography,
  Card,
  CardContent,
  Grid,
  LinearProgress,
  Chip,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
} from '@mui/material'

const DriveMigration: React.FC = () => {
  return (
    <Box>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>Drive Migration</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Detailed file-level migration tracking</Typography>

      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} sm={6} md={3}>
          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent sx={{ p: 2 }}>
              <Typography variant="caption" color="text.secondary">Current Folder</Typography>
              <Typography variant="body1" fontWeight={600} sx={{ mt: 0.5 }}>PRJ-001-Apollo/Design</Typography>
            </CardContent>
          </Card>
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent sx={{ p: 2 }}>
              <Typography variant="caption" color="text.secondary">Current File</Typography>
              <Typography variant="body1" fontWeight={600} sx={{ mt: 0.5, wordBreak: 'break-all' }}>Dashboard_v2.pptx</Typography>
            </CardContent>
          </Card>
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent sx={{ p: 2 }}>
              <Typography variant="caption" color="text.secondary">File Size</Typography>
              <Typography variant="h6" fontWeight={700} sx={{ mt: 0.5 }}>24.5 MB</Typography>
            </CardContent>
          </Card>
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent sx={{ p: 2 }}>
              <Typography variant="caption" color="text.secondary">Upload Speed</Typography>
              <Typography variant="h6" fontWeight={700} sx={{ mt: 0.5 }}>3.2 MB/s</Typography>
            </CardContent>
          </Card>
        </Grid>
      </Grid>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardContent sx={{ p: 3 }}>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>Progress Overview</Typography>
          <Grid container spacing={3}>
            <Grid item xs={12} sm={4}>
              <Typography variant="caption" color="text.secondary">Files Completed</Typography>
              <Typography variant="h5" sx={{ fontWeight: 700 }}>89 / 198</Typography>
              <LinearProgress variant="determinate" value={45} sx={{ height: 8, borderRadius: 4, mt: 1 }} />
            </Grid>
            <Grid item xs={12} sm={4}>
              <Typography variant="caption" color="text.secondary">Files Remaining</Typography>
              <Typography variant="h5" sx={{ fontWeight: 700 }}>109</Typography>
              <LinearProgress variant="determinate" value={55} sx={{ height: 8, borderRadius: 4, mt: 1 }} />
            </Grid>
            <Grid item xs={12} sm={4}>
              <Typography variant="caption" color="text.secondary">Estimated Remaining</Typography>
              <Typography variant="h5" sx={{ fontWeight: 700 }}>~18 min</Typography>
            </Grid>
          </Grid>
        </CardContent>
      </Card>

      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} sm={6}>
          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent>
              <Typography variant="subtitle2" fontWeight={600} gutterBottom>Permissions Restored</Typography>
              <Typography variant="h4" sx={{ fontWeight: 700, color: 'success.main' }}>12 / 12</Typography>
              <Typography variant="caption" color="text.secondary">100% complete</Typography>
            </CardContent>
          </Card>
        </Grid>
        <Grid item xs={12} sm={6}>
          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent>
              <Typography variant="subtitle2" fontWeight={600} gutterBottom>Sharing Restored</Typography>
              <Typography variant="h4" sx={{ fontWeight: 700, color: 'success.main' }}>8 / 8</Typography>
              <Typography variant="caption" color="text.secondary">100% complete</Typography>
            </CardContent>
          </Card>
        </Grid>
        <Grid item xs={12} sm={6}>
          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent>
              <Typography variant="subtitle2" fontWeight={600} gutterBottom>Versions Restored</Typography>
              <Typography variant="h4" sx={{ fontWeight: 700, color: 'success.main' }}>45 / 45</Typography>
              <Typography variant="caption" color="text.secondary">100% complete</Typography>
            </CardContent>
          </Card>
        </Grid>
        <Grid item xs={12} sm={6}>
          <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <CardContent>
              <Typography variant="subtitle2" fontWeight={600} gutterBottom>Retry Count</Typography>
              <Typography variant="h4" sx={{ fontWeight: 700 }}>0</Typography>
              <Typography variant="caption" color="text.secondary">No retries needed</Typography>
            </CardContent>
          </Card>
        </Grid>
      </Grid>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <CardContent>
          <Typography variant="subtitle2" fontWeight={600} gutterBottom>Recent API Calls</Typography>
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Time</TableCell>
                  <TableCell>Method</TableCell>
                  <TableCell>Endpoint</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell>Duration</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                <TableRow>
                  <TableCell>10:42:31</TableCell>
                  <TableCell>POST</TableCell>
                  <TableCell>/upload/drive/files</TableCell>
                  <TableCell><Chip label="200 OK" size="small" color="success" variant="outlined" /></TableCell>
                  <TableCell>1.2s</TableCell>
                </TableRow>
                <TableRow>
                  <TableCell>10:42:28</TableCell>
                  <TableCell>GET</TableCell>
                  <TableCell>/drive/v2/files/fileId</TableCell>
                  <TableCell><Chip label="200 OK" size="small" color="success" variant="outlined" /></TableCell>
                  <TableCell>0.4s</TableCell>
                </TableRow>
                <TableRow>
                  <TableCell>10:42:25</TableCell>
                  <TableCell>POST</TableCell>
                  <TableCell>/drive/v2/files/fileId/permissions</TableCell>
                  <TableCell><Chip label="200 OK" size="small" color="success" variant="outlined" /></TableCell>
                  <TableCell>0.8s</TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </TableContainer>
        </CardContent>
      </Card>
    </Box>
  )
}

export default DriveMigration