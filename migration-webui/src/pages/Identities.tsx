import React, { useCallback, useEffect, useState } from 'react'
import {
  Box, Typography, Card, CardContent, CardActionArea, Stack, TextField, Button, Alert,
  Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Chip,
  IconButton, Tooltip, Grid, LinearProgress, Collapse, CircularProgress, Divider,
} from '@mui/material'
import {
  Refresh as RefreshIcon, People as IdentitiesIcon,
  Language as DomainIcon, ExpandMore as ExpandIcon,
} from '@mui/icons-material'
import { fetchActions, fetchIdentities, saveIdentityPair, IdentityRow, ActionSpec } from '@/api/client'
import {
  fetchVerifiedDomains, VerifiedDomain,
  fetchTenantInventory, TenantInventory,
} from '@/api/controlPlane'
import JobRunner from '@/components/JobRunner'

/**
 * Operator/superadmin-only: what init-db has actually loaded
 * (identity_map, the read side) plus what it will load next time
 * (identities.csv, via init_db/init_db_auto below -- the write side).
 * Replaces the legacy dashboard's identities tab, which called
 * GET /api/identities and POST /api/identities/save -- neither route
 * existed server-side, so this capability was never actually shipped.
 */
const DOMAIN_STATUS: Record<VerifiedDomain['status'],
  { label: string; color: 'success' | 'warning' | 'error' | 'default' }> = {
  verified: { label: 'Verified', color: 'success' },
  pending: { label: 'Propagating', color: 'warning' },
  not_verified: { label: 'Not verified', color: 'default' },
  not_set_up: { label: 'Not set up', color: 'default' },
  error: { label: 'Error', color: 'error' },
}

/** One scoped tenant: its domain, which side it is, the admin it signs in
 *  as, and how many delegation scopes are actually live. The same
 *  functional check the Jobs page cards use, put here because a user mapping
 *  is only meaningful once the domains it maps between are actually set up. */
const GB = (bytes: number) => (bytes / 1e9)

/** A small labelled figure in the stats grid. */
const Stat: React.FC<{ label: string; value: React.ReactNode }> = ({ label, value }) => (
  <Box>
    <Typography sx={{ fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>
      {value}
    </Typography>
    <Typography variant="caption" color="text.secondary">{label}</Typography>
  </Box>
)

const DomainCard: React.FC<{ d: VerifiedDomain }> = ({ d }) => {
  const st = DOMAIN_STATUS[d.status] ?? DOMAIN_STATUS.error
  const frac = d.total > 0 ? d.live / d.total : 0
  const setUp = d.total > 0 && !d.error
  const [open, setOpen] = useState(false)
  const [inv, setInv] = useState<TenantInventory | null>(null)
  const [busy, setBusy] = useState(false)
  const [invErr, setInvErr] = useState('')

  const toggle = () => {
    const next = !open
    setOpen(next)
    // Fetched on first open, not on render: this is two live Google calls
    // per account, so it must never ride a poll or a page load -- a click
    // is the explicit trigger it needs.
    if (next && setUp && !inv && !busy) {
      setBusy(true); setInvErr('')
      fetchTenantInventory(d.side)
        .then(setInv)
        .catch((e) => setInvErr(e instanceof Error ? e.message : String(e)))
        .finally(() => setBusy(false))
    }
  }

  const licences = Object.entries(inv?.licenseCounts || {})

  return (
    <Card elevation={0} data-testid={`domain-card-${d.side}`}
          sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider',
                height: '100%' }}>
      <CardActionArea onClick={toggle} data-testid={`domain-card-open-${d.side}`}
                      sx={{ p: 0 }}>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
            <DomainIcon fontSize="small" color="action" />
            <Chip size="small" label={d.side} variant="outlined"
                  sx={{ textTransform: 'capitalize' }} />
            <Box sx={{ flexGrow: 1 }} />
            <Chip size="small" label={st.label} color={st.color}
                  variant={st.color === 'default' ? 'outlined' : 'filled'} />
            <ExpandIcon fontSize="small" color="action"
                        sx={{ transform: open ? 'rotate(180deg)' : 'none',
                              transition: '0.2s' }} />
          </Stack>
          <Typography sx={{ fontWeight: 700, wordBreak: 'break-all' }}>
            {d.domain || '(not set up)'}
          </Typography>
          {d.adminEmail && (
            <Typography variant="body2" color="text.secondary"
                        sx={{ wordBreak: 'break-all', mb: 1 }}>
              {d.adminEmail}
            </Typography>
          )}
          {d.error ? (
            <Alert severity="warning" sx={{ mt: 1 }}>{d.error}</Alert>
          ) : setUp ? (
            <Box sx={{ mt: 1 }}>
              <Stack direction="row" justifyContent="space-between" sx={{ mb: 0.5 }}>
                <Typography variant="caption" color="text.secondary">
                  Delegation scopes
                </Typography>
                <Typography variant="caption"
                            sx={{ fontVariantNumeric: 'tabular-nums' }}>
                  {d.live}/{d.total} live
                </Typography>
              </Stack>
              <LinearProgress variant="determinate" value={frac * 100}
                              color={frac >= 1 ? 'success' : frac > 0 ? 'warning' : 'error'}
                              sx={{ height: 6, borderRadius: 1 }} />
            </Box>
          ) : (
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
              No delegation scopes yet — finish it in the Setup Wizard.
            </Typography>
          )}
        </CardContent>
      </CardActionArea>

      <Collapse in={open} unmountOnExit>
        <Divider />
        <Box sx={{ p: 2 }} data-testid={`domain-stats-${d.side}`}>
          {!setUp ? (
            <Typography variant="body2" color="text.secondary">
              This tenant is not set up yet, so there is nothing to read.
              Finish it in the Setup Wizard, then its stats appear here.
            </Typography>
          ) : busy ? (
            <Stack direction="row" spacing={1} alignItems="center">
              <CircularProgress size={16} />
              <Typography variant="body2" color="text.secondary">
                Reading the tenant live…
              </Typography>
            </Stack>
          ) : invErr ? (
            <Alert severity="warning">{invErr}</Alert>
          ) : inv ? (
            <>
              <Grid container spacing={2} sx={{ mb: licences.length ? 1.5 : 0 }}>
                <Grid item xs={6} sm={3}>
                  <Stat label="Users" value={inv.accounts.toLocaleString()} />
                </Grid>
                <Grid item xs={6} sm={3}>
                  <Stat label="Drive"
                        value={`${GB(inv.totals.driveBytes).toFixed(1)} GB`} />
                </Grid>
                <Grid item xs={6} sm={3}>
                  <Stat label="Email" value={inv.totals.emails.toLocaleString()} />
                </Grid>
                <Grid item xs={6} sm={3}>
                  <Stat label="Measured from"
                        value={`${inv.totals.covered}/${inv.accounts}`} />
                </Grid>
              </Grid>

              <Typography variant="caption" color="text.secondary"
                          sx={{ fontWeight: 600 }}>
                Licences (assigned)
              </Typography>
              {inv.licenseError ? (
                <Typography variant="body2" color="text.secondary">
                  Couldn&apos;t read licences — the licensing scope isn&apos;t granted.
                </Typography>
              ) : licences.length ? (
                <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 0.75, mt: 0.5 }}>
                  {licences.map(([sku, n]) => (
                    <Chip key={sku} size="small" variant="outlined"
                          label={`${sku} · ${n.toLocaleString()}`} />
                  ))}
                </Stack>
              ) : (
                <Typography variant="body2" color="text.secondary">
                  No licences assigned.
                </Typography>
              )}
              <Typography variant="caption" color="text.secondary"
                          sx={{ display: 'block', mt: 1.5 }}>
                Google only reports licences <em>in use</em> per SKU; free seats
                need a Reseller scope this tool doesn&apos;t request, so those
                counts are what is assigned, not what remains.
              </Typography>
              {inv.truncated && (
                <Typography variant="caption" color="warning.main"
                            sx={{ display: 'block', mt: 1 }}>
                  Showing the first {inv.users.length} of {inv.accounts} accounts.
                </Typography>
              )}
            </>
          ) : null}
        </Box>
      </Collapse>
    </Card>
  )
}

const Identities: React.FC = () => {
  const [rows, setRows] = useState<IdentityRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [actions, setActions] = useState<Record<string, ActionSpec>>({})
  const [source, setSource] = useState('')
  const [target, setTarget] = useState('')
  const [addErr, setAddErr] = useState<string | null>(null)
  const [addOk, setAddOk] = useState<string | null>(null)
  const [domains, setDomains] = useState<VerifiedDomain[]>([])

  const refresh = useCallback(() => {
    setLoading(true); setError(null)
    fetchIdentities()
      .then(setRows)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
    // Two live Google calls per side, but the caller already asked for a
    // refresh here, so it rides the same explicit trigger rather than a poll.
    fetchVerifiedDomains().then((r) => setDomains(r.domains)).catch(() => setDomains([]))
  }, [])

  useEffect(() => { refresh(); fetchActions().then(setActions) }, [refresh])

  const addPair = async () => {
    setAddErr(null); setAddOk(null)
    const r = await saveIdentityPair(source, target)
    if (r.ok) {
      setAddOk(`saved -- ${r.total} pair(s) in identities.csv`)
      setSource(''); setTarget('')
    } else {
      setAddErr(r.error || 'could not save')
    }
  }

  return (
    <Box>
      <Stack direction="row" alignItems="center" sx={{ mb: 0.5 }}>
        <IdentitiesIcon color="action" sx={{ mr: 1 }} />
        <Typography variant="h4" sx={{ fontWeight: 700, flexGrow: 1 }}>Identities</Typography>
        <Tooltip title="Re-check">
          <span>
            <IconButton size="small" onClick={refresh} disabled={loading}>
              <RefreshIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        The source→target user mapping every migration action needs.
      </Typography>

      {domains.length > 0 && (
        <Box sx={{ mb: 3 }} data-testid="scoped-domains">
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 1.5 }}>
            Scoped domains
          </Typography>
          <Grid container spacing={2}>
            {domains.map((d) => (
              <Grid item xs={12} sm={6} md={4} key={`${d.side}-${d.domain}`}>
                <DomainCard d={d} />
              </Grid>
            ))}
          </Grid>
        </Box>
      )}

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardContent>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 1.5 }}>Load into identity_map</Typography>
          <Stack spacing={3}>
            {actions.init_db_auto && (
              <JobRunner name="init_db_auto" spec={actions.init_db_auto} onDone={refresh} />
            )}
            {actions.init_db && (
              <JobRunner name="init_db" spec={actions.init_db} onDone={refresh} />
            )}
          </Stack>
        </CardContent>
      </Card>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardContent>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 1.5 }}>Add one pair by hand</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Appends to identities.csv -- takes effect next time either
            action above runs, not immediately.
          </Typography>
          <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 2 }}>
            <TextField size="small" label="Source email" value={source}
                       onChange={(e) => setSource(e.target.value)} sx={{ width: 260 }} />
            <TextField size="small" label="Target email" value={target}
                       onChange={(e) => setTarget(e.target.value)} sx={{ width: 260 }} />
            <Button variant="contained" disabled={!source || !target} onClick={addPair}>
              Add pair
            </Button>
          </Stack>
          {addOk && <Alert severity="success" sx={{ mt: 2 }}>{addOk}</Alert>}
          {addErr && <Alert severity="error" sx={{ mt: 2 }}>{addErr}</Alert>}
        </CardContent>
      </Card>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <CardContent>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>
            Currently loaded ({rows.length})
          </Typography>
          {error && <Alert severity="warning" sx={{ mb: 2 }}>{error}</Alert>}
          <TableContainer sx={{ maxHeight: 480 }}>
            <Table stickyHeader size="small">
              <TableHead>
                <TableRow>
                  <TableCell sx={{ fontWeight: 600 }}>Source</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Target</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Type</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Status</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map((r) => (
                  <TableRow key={r.source_email}>
                    <TableCell>{r.source_email}</TableCell>
                    <TableCell>{r.target_email}</TableCell>
                    <TableCell>{r.entity_type}</TableCell>
                    <TableCell>
                      <Chip size="small" label={r.status} variant="outlined"
                            color={r.status === 'DONE' ? 'success' : r.status === 'FAILED' ? 'error' : 'default'} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
          {!loading && rows.length === 0 && !error && (
            <Typography variant="body2" color="text.secondary" sx={{ py: 3, textAlign: 'center' }}>
              Nothing loaded yet -- run one of the actions above.
            </Typography>
          )}
        </CardContent>
      </Card>
    </Box>
  )
}

export default Identities
