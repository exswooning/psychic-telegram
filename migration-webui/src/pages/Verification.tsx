import React, { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
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
  Stack,
  IconButton,
  Tooltip,
  Menu,
  MenuItem,
  ListItemIcon,
  ListItemText,
} from '@mui/material'
import {
  CheckCircle as VerifiedIcon, Warning as MismatchIcon, HourglassEmpty as PendingIcon,
  Block as NotStartedIcon, Assessment as ScoreIcon, Refresh as RefreshIcon,
  Language as DomainIcon, Grass as SeedIcon, RocketLaunch as MigrateIcon,
} from '@mui/icons-material'
import { useMigrationStore } from '@/store'
import { statusLabel, statusColor } from '@/utils/formatters'
import { fetchVerifiedDomains, VerifiedDomain, fetchMe } from '@/api/controlPlane'

/**
 * Which domain(s) this account has actually finished setting up (via the
 * Setup/Seed Wizard) and can use right now -- distinct from the data
 * reconciliation checks below, which need a completed migration run to
 * mean anything. This needs only a completed DWD grant: it re-asks Google
 * whether tokens for the required scopes still issue, the same functional
 * check dwd_status runs, so "verified" here means genuinely working, not
 * just "a domain was typed into a form once".
 */
const VERIFIED_DOMAIN_LABEL: Record<VerifiedDomain['status'], string> = {
  verified: 'Verified', pending: 'Propagating', not_verified: 'Not verified',
  not_set_up: 'Not set up', error: 'Check failed',
}
const VERIFIED_DOMAIN_COLOR: Record<VerifiedDomain['status'], 'success' | 'warning' | 'error' | 'default'> = {
  verified: 'success', pending: 'warning', not_verified: 'error',
  not_set_up: 'default', error: 'error',
}

const VerifiedDomains: React.FC = () => {
  const navigate = useNavigate()
  const [domains, setDomains] = useState<VerifiedDomain[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [seedEnabled, setSeedEnabled] = useState(false)
  const [menuFor, setMenuFor] = useState<{ side: 'source' | 'target'; anchor: HTMLElement } | null>(null)

  const refresh = useCallback(() => {
    setLoading(true); setError(null)
    fetchVerifiedDomains()
      .then((r) => setDomains(r.domains))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [])

  // Auto-poll, not fetch-once -- DWD status (verified/pending/error) is
  // exactly the kind of thing that changes while this page stays open
  // (propagation finishing, a scope going live), and a one-shot fetch left
  // it showing whatever was true at page load until a manual re-check.
  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 5000)
    return () => clearInterval(id)
  }, [refresh])
  useEffect(() => { fetchMe().then((a) => setSeedEnabled(a.seed_enabled)).catch(() => {}) }, [])

  if (domains !== null && domains.length === 0 && !error) return null

  const closeMenu = () => setMenuFor(null)
  const goSeed = () => { closeMenu(); navigate('/wizard?mode=seed') }
  const goMigrate = () => { closeMenu(); navigate('/mission-control') }

  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
      <CardContent sx={{ p: 3 }}>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
          <DomainIcon color="action" fontSize="small" />
          <Typography variant="h6" sx={{ fontWeight: 600, flexGrow: 1 }}>Verified Domains</Typography>
          <Tooltip title="Re-check">
            <span>
              <IconButton size="small" onClick={refresh} disabled={loading}>
                <RefreshIcon fontSize="small" />
              </IconButton>
            </span>
          </Tooltip>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Domains set up through the wizard, with a live check of whether
          their domain-wide delegation is actually working -- not just
          granted, but still issuing tokens right now. Click a domain for
          seed/migrate options.
        </Typography>

        {loading && domains === null && <LinearProgress sx={{ mb: 1 }} />}
        {error && <Alert severity="warning">{error}</Alert>}

        {domains && domains.length > 0 && (
          <Stack spacing={1}>
            {domains.map((d) => (
              <Box key={d.side}
                   onClick={(e) => setMenuFor({ side: d.side, anchor: e.currentTarget })}
                   sx={{
                     display: 'flex', alignItems: 'center', gap: 1.5, p: 1.5,
                     borderRadius: 1, bgcolor: 'action.hover', flexWrap: 'wrap',
                     cursor: 'pointer', '&:hover': { bgcolor: 'action.selected' },
                   }}>
                <Chip size="small" label={d.side} variant="outlined" sx={{ textTransform: 'capitalize' }} />
                <Typography variant="body2" sx={{ fontWeight: 600, flexGrow: 1, minWidth: 160 }}>
                  {d.domain}
                </Typography>
                {d.total > 0 && (
                  <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                    {d.live}/{d.total} scopes
                  </Typography>
                )}
                <Chip size="small" label={VERIFIED_DOMAIN_LABEL[d.status]}
                      color={VERIFIED_DOMAIN_COLOR[d.status]}
                      variant={d.status === 'verified' ? 'filled' : 'outlined'} />
              </Box>
            ))}
          </Stack>
        )}

        <Menu anchorEl={menuFor?.anchor} open={!!menuFor} onClose={closeMenu}>
          {menuFor?.side === 'source' && seedEnabled && (
            <MenuItem onClick={goSeed}>
              <ListItemIcon><SeedIcon fontSize="small" /></ListItemIcon>
              <ListItemText>Seed this tenant</ListItemText>
            </MenuItem>
          )}
          <MenuItem onClick={goMigrate}>
            <ListItemIcon><MigrateIcon fontSize="small" /></ListItemIcon>
            <ListItemText>Migrate</ListItemText>
          </MenuItem>
        </Menu>
      </CardContent>
    </Card>
  )
}

const Verification: React.FC = () => {
  const { verification } = useMigrationStore()
  const overallConfidence = verification.length > 0
    ? Math.round(verification.reduce((sum, v) => sum + v.confidence, 0) / verification.length)
    : 0

  const formatAge = (seconds: number): string => {
    if (seconds < 3600) return `${Math.round(seconds / 60)}m old`
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h old`
    return `${Math.round(seconds / 86400)}d old`
  }
  // acl_audit.py is a standalone script that never runs automatically during
  // migrate/delta, so this row's numbers can describe a run from days ago
  // rather than what is happening right now -- flagged past 10 minutes so a
  // stale snapshot is never mistaken for live progress.
  const isStale = (seconds?: number | null) => seconds != null && seconds > 600

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

      <VerifiedDomains />

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
                <Box sx={{ display: 'flex', gap: 0.5, mt: 1, flexWrap: 'wrap' }}>
                  <Chip label={statusLabel(item.status)} size="small" color={statusColor(item.status) as any} variant="outlined" />
                  {isStale(item.ageSeconds) && (
                    <Chip label={`stale · ${formatAge(item.ageSeconds!)}`} size="small" color="warning" variant="outlined" />
                  )}
                </Box>
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
                      {isStale(item.ageSeconds) && (
                        <Typography variant="caption" color="warning.main" display="block">
                          stale snapshot, {formatAge(item.ageSeconds!)} · not live
                        </Typography>
                      )}
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