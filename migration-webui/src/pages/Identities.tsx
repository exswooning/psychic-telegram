import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Box, Typography, Card, CardContent, CardActionArea, Stack, TextField, Button, Alert,
  Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Chip,
  IconButton, Tooltip, Grid, LinearProgress, Collapse, CircularProgress, Divider,
  Dialog, DialogTitle, DialogContent, DialogActions,
} from '@mui/material'
import {
  Refresh as RefreshIcon, People as IdentitiesIcon,
  Language as DomainIcon, ExpandMore as ExpandIcon,
  DeleteOutline as DeleteIcon,
} from '@mui/icons-material'
import {
  fetchActions, fetchIdentities, saveIdentityPair, IdentityRow, ActionSpec,
  removeTenantSetup,
} from '@/api/client'
import {
  fetchVerifiedDomains, VerifiedDomain,
  fetchTenantInventory, TenantInventory,
  fetchAllDomains, ConfiguredDomain,
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

/** The live stats for one tenant, fetched when this mounts (i.e. when the
 *  card opens, since the Collapse is unmountOnExit). accountId lets a
 *  superadmin read a tenant belonging to ANOTHER account -- the "all
 *  configured domains" cards span accounts; omitted, it is the caller's own. */
const TenantStats: React.FC<{
  side: 'source' | 'target'; accountId?: number; canRead: boolean; testId: string
}> = ({ side, accountId, canRead, testId }) => {
  const [inv, setInv] = useState<TenantInventory | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => {
    // Mounted only while open, so this IS the click-triggered fetch -- two
    // live Google calls per account, never a page load or a poll.
    if (!canRead) return
    setBusy(true); setErr('')
    fetchTenantInventory(side, 250, false, accountId)
      .then(setInv)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false))
  }, [side, accountId, canRead])
  const licences = Object.entries(inv?.licenseCounts || {})
  // The read can fail two ways: the request rejects (err), OR it returns 200
  // with an error field -- snapshot() catches a dead delegation and reports
  // it in the body rather than raising, so a card that only checked the
  // reject path rendered "0 users / 0 GB", which reads as an empty tenant
  // rather than an unreadable one. Treat both the same.
  const failure = err || inv?.error || ''
  return (
    <Box sx={{ p: 2 }} data-testid={testId}>
      {!canRead ? (
        <Typography variant="body2" color="text.secondary">
          This tenant is not set up yet, so there is nothing to read. Finish it
          in the Setup Wizard, then its stats appear here.
        </Typography>
      ) : busy ? (
        <Stack direction="row" spacing={1} alignItems="center">
          <CircularProgress size={16} />
          <Typography variant="body2" color="text.secondary">
            Reading the tenant live…
          </Typography>
        </Stack>
      ) : failure ? (
        <Alert severity="warning">
          {/(invalid_grant|unauthorized_client|no valid verifier)/i.test(failure)
            ? 'Its stats cannot be read because the domain-wide delegation '
              + 'is not live — the service account has a key here, but its '
              + 'client ID was never granted (or was revoked) in this '
              + "tenant's Admin Console. Re-run setup, or grant delegation "
              + 'for the client ID, and the stats will appear.'
            : failure}
        </Alert>
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
  )
}

const DomainCard: React.FC<{ d: VerifiedDomain }> = ({ d }) => {
  const st = DOMAIN_STATUS[d.status] ?? DOMAIN_STATUS.error
  const frac = d.total > 0 ? d.live / d.total : 0
  const setUp = d.total > 0 && !d.error
  const [open, setOpen] = useState(false)
  const [delOpen, setDelOpen] = useState(false)

  return (
    <Card elevation={0} data-testid={`domain-card-${d.side}`}
          sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider',
                height: '100%' }}>
      <CardActionArea onClick={() => setOpen((v) => !v)} data-testid={`domain-card-open-${d.side}`}
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
        <TenantStats side={d.side} canRead={setUp}
                     testId={`domain-stats-${d.side}`} />
        <Box sx={{ px: 2, pb: 2 }}>
          <Button size="small" color="error" startIcon={<DeleteIcon />}
                  data-testid={`delete-domain-${d.side}`}
                  onClick={() => setDelOpen(true)}>
            Delete this setup
          </Button>
        </Box>
      </Collapse>
      <DeleteSetupDialog open={delOpen} onClose={() => setDelOpen(false)}
                         side={d.side} domain={d.domain} />
    </Card>
  )
}

/** A configured domain in the "all configured domains" list, clickable to
 *  its live stats. accountId is passed through so a superadmin reads the
 *  right tenant, not the caller's own. */
const ConfigDomainCard: React.FC<{ d: ConfiguredDomain }> = ({ d }) => {
  const [open, setOpen] = useState(false)
  const [delOpen, setDelOpen] = useState(false)
  // A superseded domain's live stats can't be read: the inventory endpoint
  // reads the slot's ACTIVE domain, which is the one that replaced this. So
  // its card explains what happened instead of fetching a stranger's numbers.
  return (
    <Card elevation={0} data-testid={`config-${d.accountId}-${d.side}`}
          sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider',
                height: '100%', opacity: d.superseded ? 0.85 : 1 }}>
      <CardActionArea onClick={() => setOpen((v) => !v)}
                      data-testid={`config-open-${d.accountId}-${d.side}`}>
        <CardContent sx={{ py: 1.5 }}>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
            <Chip size="small" label={d.side} variant="outlined"
                  sx={{ textTransform: 'capitalize' }} />
            <Box sx={{ flexGrow: 1 }} />
            {d.superseded ? (
              <Chip size="small" label="Superseded" color="warning" variant="outlined" />
            ) : (
              <Chip size="small" label={d.hasKey ? 'Key on file' : 'No key'}
                    color={d.hasKey ? 'success' : 'default'}
                    variant={d.hasKey ? 'filled' : 'outlined'} />
            )}
            <ExpandIcon fontSize="small" color="action"
                        sx={{ transform: open ? 'rotate(180deg)' : 'none',
                              transition: '0.2s' }} />
          </Stack>
          <Typography sx={{ fontWeight: 700, wordBreak: 'break-all',
                            textDecoration: d.superseded ? 'line-through' : 'none' }}>
            {d.domain}
          </Typography>
          <Typography variant="body2" color="text.secondary"
                      sx={{ wordBreak: 'break-all' }}>
            {d.adminEmail}
          </Typography>
        </CardContent>
      </CardActionArea>
      <Collapse in={open} unmountOnExit>
        <Divider />
        {d.superseded ? (
          <Box sx={{ p: 2 }} data-testid={`config-stats-${d.accountId}-${d.side}`}>
            <Typography variant="body2" color="text.secondary">
              This domain was replaced in its slot{d.replacedBy
                ? <> by <strong>{d.replacedBy}</strong></> : null}. It is kept
              here so nothing you set up disappears, and its key is backed up
              {d.hasKey ? ' on disk' : ' (the key was not preserved)'} — but its
              live stats can no longer be read, because the slot now points at
              the domain that replaced it.
            </Typography>
          </Box>
        ) : (
          <TenantStats side={d.side} accountId={d.accountId} canRead={d.hasKey}
                       testId={`config-stats-${d.accountId}-${d.side}`} />
        )}
        {!d.superseded && (
          <Box sx={{ px: 2, pb: 2 }}>
            <Button size="small" color="error" startIcon={<DeleteIcon />}
                    data-testid={`delete-config-${d.accountId}-${d.side}`}
                    onClick={() => setDelOpen(true)}>
              Delete this setup
            </Button>
          </Box>
        )}
      </Collapse>
      <DeleteSetupDialog open={delOpen} onClose={() => setDelOpen(false)}
                         side={d.side} domain={d.domain} accountId={d.accountId} />
    </Card>
  )
}

/** Delete a domain's setup: undo exactly what the Setup Wizard created --
 *  the Cloud project, the delegation grant, the config and the key -- and
 *  nothing else. The tenant's own data is left untouched (the wizard never
 *  made any). Irreversible; you re-run the wizard to set it up again. */
const DeleteSetupDialog: React.FC<{
  open: boolean; onClose: () => void
  side: 'source' | 'target'; domain: string; accountId?: number
}> = ({ open, onClose, side, domain, accountId }) => {
  const [typed, setTyped] = useState('')
  const [password, setPassword] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [started, setStarted] = useState(false)

  useEffect(() => {
    if (open) {
      setTyped(''); setPassword(''); setReason(''); setErr(''); setStarted(false)
    }
  }, [open])

  const ready = typed.trim().toLowerCase() === domain.toLowerCase()
    && password.length > 0 && reason.trim().length >= 3

  const remove = async () => {
    setBusy(true); setErr('')
    try {
      const r = await removeTenantSetup(side, domain, password, 'remove_setup', accountId)
      if (!r.ok) throw new Error(r.error || 'could not remove the setup')
      setStarted(true)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} fullWidth maxWidth="sm">
      <DialogTitle>Delete {domain}</DialogTitle>
      <DialogContent>
        {started ? (
          <Alert severity="success" data-testid="delete-started">
            Removal started. Deleting the Cloud project and revoking delegation
            run in the background and take a few minutes — this domain
            disappears from the list here once it finishes. Watch progress on
            the Jobs page.
          </Alert>
        ) : (
          <>
            <Alert severity="error" sx={{ mb: 2 }}>
              This revokes the delegation, deletes the Cloud project, and forgets
              the config and key for <strong>{domain}</strong> — everything the
              Setup Wizard created. The tenant&apos;s own Workspace data is NOT
              touched. It cannot be undone; you would re-run the wizard to set it
              up again.
            </Alert>
            {err && <Alert severity="warning" sx={{ mb: 2 }} data-testid="delete-error">{err}</Alert>}
            <Stack spacing={2}>
              <TextField fullWidth label="Type the domain to confirm" value={typed}
                         onChange={(e) => setTyped(e.target.value)}
                         placeholder={domain}
                         inputProps={{ 'data-testid': 'delete-confirm-domain' }} />
              <TextField fullWidth type="password" label="Admin password" value={password}
                         onChange={(e) => setPassword(e.target.value)}
                         helperText="needed to delete the Cloud project and revoke delegation"
                         inputProps={{ 'data-testid': 'delete-password',
                                       autoComplete: 'off' }} />
              <TextField fullWidth label="Reason" value={reason}
                         onChange={(e) => setReason(e.target.value)}
                         inputProps={{ 'data-testid': 'delete-reason' }} />
            </Stack>
          </>
        )}
      </DialogContent>
      <DialogActions>
        {started ? (
          <Button onClick={onClose} variant="contained">Close</Button>
        ) : (
          <>
            <Button onClick={onClose} disabled={busy}>Cancel</Button>
            <Button color="error" variant="contained" onClick={remove}
                    disabled={!ready || busy} data-testid="delete-go">
              {busy ? 'Removing…' : 'Delete setup'}
            </Button>
          </>
        )}
      </DialogActions>
    </Dialog>
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
  const [allDomains, setAllDomains] = useState<ConfiguredDomain[]>([])
  const [allSuper, setAllSuper] = useState(false)
  const [domainsLoading, setDomainsLoading] = useState(true)

  // The configured-domain list is config-only (no Google call), so it can
  // poll cheaply -- that is what makes a domain you just set up appear here
  // on its own, which the slow live check below cannot afford to.
  // The slow live delegation check, on its own so the poll can re-run it
  // ONLY when a domain actually changed -- never every tick.
  const refreshVerified = useCallback(() => {
    setDomainsLoading(true)
    fetchVerifiedDomains()
      .then((r) => setDomains(r.domains))
      .catch(() => setDomains([]))
      .finally(() => setDomainsLoading(false))
  }, [])

  // A fingerprint of the config list, so the poll can tell "nothing changed"
  // from "a domain was added or swapped" without re-running the live check
  // on every 8s tick.
  const configSig = useRef('')

  const refreshAllDomains = useCallback(() => {
    fetchAllDomains().then((r) => {
      setAllDomains(r.domains); setAllSuper(r.superadmin)
      const sig = r.domains
        .map((d) => `${d.accountId}:${d.side}:${d.domain}:${d.hasKey}`)
        .sort().join('|')
      // First load seeds the fingerprint; a later change re-runs the slow
      // live check so a domain you just set up shows its real status on its
      // own, instead of waiting for a manual refresh.
      if (configSig.current && sig !== configSig.current) refreshVerified()
      configSig.current = sig
    }).catch(() => { /* leave the last good list up */ })
  }, [refreshVerified])

  const refresh = useCallback(() => {
    setLoading(true); setError(null)
    fetchIdentities()
      .then(setRows)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
    // Two live Google calls PER SCOPE per side -- it asks Google whether
    // each delegated token actually works, not whether a flag is set -- so
    // it takes a few seconds and earns a spinner. It rides this explicit
    // refresh, never the poll below.
    refreshVerified()
    refreshAllDomains()
  }, [refreshAllDomains, refreshVerified])

  useEffect(() => { refresh(); fetchActions().then(setActions) }, [refresh])

  // Poll only the cheap config list, so a domain added elsewhere shows up
  // here within a few seconds without re-running the slow live delegation
  // check on every tick.
  useEffect(() => {
    const t = window.setInterval(refreshAllDomains, 8000)
    return () => window.clearInterval(t)
  }, [refreshAllDomains])

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

      {domainsLoading && domains.length === 0 && (
        <Box sx={{ mb: 3 }} data-testid="scoped-domains-loading">
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 1.5 }}>
            Scoped domains
          </Typography>
          <Stack direction="row" spacing={1.5} alignItems="center"
                 sx={{ p: 2, border: '1px solid', borderColor: 'divider',
                       borderRadius: 2 }}>
            <CircularProgress size={20} />
            <Typography variant="body2" color="text.secondary">
              Checking delegation live with Google — a few seconds per side,
              because it verifies each scope's token actually works.
            </Typography>
          </Stack>
        </Box>
      )}

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

      {allSuper && allDomains.length > 0 && (
        <Box sx={{ mb: 3 }} data-testid="all-domains">
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 0.5 }}>
            All configured domains
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            Every tenant set up on this box, across accounts — a setup
            overwrites the role it targets, and a tenant can be set up under a
            different account, so this is where a domain that looks
            &quot;missing&quot; above actually is.
          </Typography>
          {Object.entries(
            allDomains.reduce((acc, d) => {
              (acc[d.accountEmail || `account #${d.accountId}`] ||= []).push(d)
              return acc
            }, {} as Record<string, ConfiguredDomain[]>),
          ).map(([account, list]) => (
            <Box key={account} sx={{ mb: 2 }}>
              <Typography variant="caption" color="text.secondary"
                          sx={{ fontWeight: 700 }}>
                {account}
              </Typography>
              <Grid container spacing={1.5} sx={{ mt: 0 }}>
                {list.map((d) => (
                  <Grid item xs={12} sm={6} md={4} key={`${d.accountId}-${d.side}`}>
                    <ConfigDomainCard d={d} />
                  </Grid>
                ))}
              </Grid>
            </Box>
          ))}
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
