import React from 'react'
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
  Avatar,
} from '@mui/material'
import { CheckCircle as VerifiedIcon, Warning as MismatchIcon, HourglassEmpty as PendingIcon, Block as NotStartedIcon, Assessment as ScoreIcon } from '@mui/icons-material'
import { useMigrationStore } from '@/store'
import { statusLabel, statusColor } from '@/utils/formatters'

const Verification: React.FC = () => {
  const { verification } = useMigrationStore()
  const overallConfidence = verification.length > 0
    ? Math.round(verification.reduce((sum, v) => sum + v.confidence, 0) / verification.length)
    : 0

  const statusIcon = (status: string) => {
    switch (status) {
      case 'verified': return <VerifiedIcon color="success" />
      case 'mismatch': return <MismatchIcon color="warning" />
      case 'pending': return <PendingIcon color="action" />
      default: return <NotStartedIcon color="disabled" />
    }
  }

  return (
    <Box>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>Verification</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Data integrity checks across all migrated services</Typography>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardContent sx={{ p: 3, display: 'flex', alignItems: 'center', gap: 3 }}>
          <Avatar sx={{ bgcolor: overallConfidence >= 95 ? 'success.light' : overallConfidence >= 80 ? 'warning.light' : 'error.light', width: 64, height: 64 }}>
            <ScoreIcon sx={{ fontSize: 32 }} />
          </Avatar>
          <Box>
            <Typography variant="caption" color="text.secondary">Overall Confidence</Typography>
            <Typography variant="h3" sx={{ fontWeight: 700 }}>{overallConfidence}%</Typography>
            <Typography variant="body2" color="text.secondary">
              {verification.filter((v) => v.status === 'verified').length} verified · {verification.filter((v) => v.status === 'mismatch').length} mismatches · {verification.filter((v) => v.status === 'pending').length} pending
            </Typography>
          </Box>
        </CardContent>
      </Card>

      <Grid container spacing={2}>
        {verification.map((item) => (
          <Grid item xs={12} sm={6} md={4} key={item.type}>
            <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
              <CardContent>
                <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1 }}>
                  <Typography variant="subtitle2" fontWeight={600} sx={{ textTransform: 'capitalize' }}>{item.type}</Typography>
                  {statusIcon(item.status)}
                </Box>
                <LinearProgress variant="determinate" value={item.confidence} sx={{ height: 8, borderRadius: 4, mb: 1 }} />
                <Typography variant="caption" color="text.secondary">Confidence: {item.confidence}%</Typography>
                <Box sx={{ display: 'flex', justifyContent: 'space-between', mt: 1 }}>
                  <Typography variant="caption" color="text.secondary">Source: {item.sourceCount}</Typography>
                  <Typography variant="caption" color="text.secondary">Target: {item.targetCount}</Typography>
                </Box>
                <Chip label={statusLabel(item.status)} size="small" color={statusColor(item.status) as any} variant="outlined" sx={{ mt: 1 }} />
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mt: 3 }}>
        <CardContent>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>Detailed Results</Typography>
          <TableContainer>
            <Table>
              <TableHead>
                <TableRow>
                  <TableCell sx={{ fontWeight: 600 }}>Type</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Status</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Source</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Target</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Confidence</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {verification.map((item) => (
                  <TableRow key={item.type}>
                    <TableCell sx={{ fontWeight: 500 }}>{item.type}</TableCell>
                    <TableCell><Chip label={statusLabel(item.status)} size="small" color={statusColor(item.status) as any} variant="outlined" /></TableCell>
                    <TableCell>{item.sourceCount}</TableCell>
                    <TableCell>{item.targetCount}</TableCell>
                    <TableCell>
                      <LinearProgress variant="determinate" value={item.confidence} sx={{ height: 6, borderRadius: 3, width: 100 }} />
                      <Typography variant="caption">{item.confidence}%</Typography>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        </CardContent>
      </Card>
    </Box>
  )
}

export default Verification