/**
 * Tally: does each user's exhaustive count agree, on both tenants?
 *
 * Every service, every item -- not a sample. Runs as each user's migration finishes
 * (tally.tally_user_and_save, started from main.migrate_user), and again on demand.
 * Distinct from the One-to-one page: that opens a bounded sample of items and compares
 * them directly; this counts everything and reads parity (target over source, once
 * items the engine deliberately skipped are subtracted). It never touches run_fidelity,
 * the whole-tenant number "Run tally" on the reports panel writes for the report itself.
 *
 * Same two rules as One-to-one. A user nobody has tallied is NOT TALLIED, in grey --
 * never a blank that reads as fine. And UNKNOWN (a tally ran but nothing could be
 * counted) is amber, never green: a check that could not be made is not a pass.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Alert, Box, Button, Checkbox, Chip, Collapse, IconButton, Paper, Stack, Table,
  TableBody, TableCell, TableHead, TableRow, ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from '@mui/material'
import { KeyboardArrowDown as OpenIcon, KeyboardArrowUp as CloseIcon, Refresh as RefreshIcon } from '@mui/icons-material'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'
import { fetchMe, fetchTally, runTally } from '@/api/controlPlane'
import type { TallyUser, TallyVerdict, TallyView } from '@/api/controlPlane'
import { ORDER, VERDICT } from '@/tallyVerdicts'

const when = (iso: string | null) => {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

const pct = (p: number | null) => (p === null ? 'not measured' : `${(p * 100).toFixed(1)}%`)

const Services: React.FC<{ u: TallyUser }> = ({ u }) => {
  const entries = Object.entries(u.services)
  if (!entries.length) {
    return <Typography variant="caption" color="text.secondary">{u.status === 'DONE' ? 'finished — not tallied yet' : `status ${u.status ?? 'unknown'}`}</Typography>
  }
  return (
    <Stack direction="row" flexWrap="wrap" gap={0.5}>
      {entries.map(([svc, c]) => (
        <Tooltip key={svc} title={`source ${c.source.toLocaleString()} · skipped ${c.skipped.toLocaleString()} · target ${c.target.toLocaleString()}${c.surplus ? ` · ${c.surplus.toLocaleString()} extra` : ''}`}>
          <Chip size="small" variant={c.parity !== null && c.parity >= 1 ? 'outlined' : 'filled'}
                color={c.parity === null ? 'warning' : c.parity >= 1 ? 'success' : 'error'}
                label={`${svc} ${pct(c.parity)}`} data-testid={`svc-${svc}`} />
        </Tooltip>
      ))}
    </Stack>
  )
}

const UserRow: React.FC<{ u: TallyUser; selected: boolean; onSelect: (on: boolean) => void }> = ({ u, selected, onSelect }) => {
  const [open, setOpen] = useState(false)
  const v = VERDICT[u.verdict]
  const hasDetail = Object.keys(u.services).length > 0 || u.worst.length > 0 || !!u.driveItems
  const items = u.driveItems
  return (
    <>
      <TableRow hover data-testid={`row-${u.user}`}>
        <TableCell padding="checkbox">
          <Checkbox size="small" checked={selected} onChange={(e) => onSelect(e.target.checked)} inputProps={{ 'aria-label': `select ${u.user}` }} />
        </TableCell>
        <TableCell>
          <Typography variant="body2">{u.user}</Typography>
          {u.target && <Typography variant="caption" color="text.secondary">→ {u.target}</Typography>}
        </TableCell>
        <TableCell>
          <Tooltip title={v.hint}><Chip size="small" color={v.color} label={v.label} sx={{ fontWeight: 700 }} data-testid={`verdict-${u.user}`} /></Tooltip>
        </TableCell>
        <TableCell><Typography variant="body2">{pct(u.countParity)}</Typography></TableCell>
        <TableCell><Services u={u} /></TableCell>
        <TableCell><Typography variant="caption">{when(u.recordedAt)}</Typography></TableCell>
        <TableCell padding="checkbox">
          {hasDetail && (
            <IconButton size="small" onClick={() => setOpen((o) => !o)} aria-label={open ? `hide ${u.user}` : `show ${u.user}`}>
              {open ? <CloseIcon fontSize="small" /> : <OpenIcon fontSize="small" />}
            </IconButton>
          )}
        </TableCell>
      </TableRow>
      {hasDetail && (
        <TableRow>
          <TableCell colSpan={7} sx={{ py: 0, borderBottom: open ? undefined : 'none' }}>
            <Collapse in={open} unmountOnExit>
              <Box sx={{ py: 1.5, pl: 2 }}>
                {u.worst.length === 0
                  ? <Typography variant="body2" color="text.secondary">Nothing short of parity.</Typography>
                  : u.worst.map((w, i) => (
                    <Typography key={i} variant="body2" sx={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>
                      {w.service}: {w.missing.toLocaleString()} missing of {w.expected.toLocaleString()} expected
                      (source {w.source.toLocaleString()}, skipped {w.skipped.toLocaleString()}, target {w.target.toLocaleString()})
                    </Typography>
                  ))}
                {items && (
                  <Box sx={{ mt: 1 }} data-testid={`items-${u.user}`}>
                    <Typography variant="body2">
                      Drive, item by item: {items.matched.toLocaleString()} of {items.compared.toLocaleString()} match
                      {items.differ ? `, ${items.differ.toLocaleString()} differ` : ''}
                      {items.missingOnTarget ? `, ${items.missingOnTarget.toLocaleString()} missing on the target` : ''}
                    </Typography>
                    {items.examples.map((e) => (
                      <Typography key={e.id} variant="body2"
                                  sx={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>
                        {e.name}: {e.why}
                      </Typography>
                    ))}
                  </Box>
                )}
              </Box>
            </Collapse>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

const Tally: React.FC = () => {
  const [params] = useSearchParams()
  const asked = Number(params.get('account')) || undefined
  const [accountId, setAccountId] = useState<number | undefined>(asked)
  const [view, setView] = useState<TallyView | null>(null)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState<'attention' | 'all'>('all')
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null)

  useEffect(() => {
    if (accountId) return
    fetchMe().then((m) => setAccountId(m.id)).catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [accountId])

  const load = useCallback(() => {
    if (!accountId) return
    fetchTally(accountId).then((v) => { setView(v); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [accountId])
  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t) }, [load])

  const users = useMemo(() => {
    const all = [...(view?.users ?? [])].sort((a, b) => ORDER.indexOf(a.verdict) - ORDER.indexOf(b.verdict) || a.user.localeCompare(b.user))
    return filter === 'all' ? all : all.filter((u) => u.verdict !== 'COMPLETE')
  }, [view, filter])

  const totals = view?.totals ?? {}
  const go = async (reason: string) => {
    setBusy(true)
    try {
      const r = await runTally(reason, { accountId, users: [...picked] })
      setNote({ ok: r.ok, text: r.detail || (r.ok ? 'started' : 'refused') })
      if (r.ok) setAsking(false)
    } catch (e) {
      setNote({ ok: false, text: e instanceof Error ? e.message : String(e) })
    } finally { setBusy(false) }
  }

  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <Typography variant="h5" sx={{ fontWeight: 700, flexGrow: 1 }}>Tally</Typography>
        <Button size="small" startIcon={<RefreshIcon />} onClick={load}>Refresh</Button>
        <Button size="small" variant="contained" onClick={() => { setNote(null); setAsking(true) }} data-testid="tally-now">
          {picked.size ? `Tally ${picked.size} selected` : 'Tally all now'}
        </Button>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 780 }}>
        {view?.onComplete
          ? 'Each user is counted on both tenants the moment their migration finishes: every item of every service, not a sample. Complete means an exact copy — every count equal. Nothing is written to either tenant.'
          : 'Every user is counted on both tenants once a migration and its repair are over (after a split run, once the DMS import has finished): every item of every service, not a sample. Complete means an exact copy — every count equal. Nothing is written to either tenant. Tally now counts sooner.'}
      </Typography>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {note && <Alert severity={note.ok ? 'success' : 'error'} sx={{ mb: 2 }} onClose={() => setNote(null)}>{note.text}</Alert>}

      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
        {ORDER.map((k) => (
          <Chip key={k} color={VERDICT[k].color} variant={(totals[k as TallyVerdict] ?? 0) ? 'filled' : 'outlined'}
                label={`${totals[k as TallyVerdict] ?? 0} ${VERDICT[k].label.toLowerCase()}`} data-testid={`total-${k}`} />
        ))}
        <Box sx={{ flexGrow: 1 }} />
        <ToggleButtonGroup size="small" exclusive value={filter} onChange={(_, v) => v && setFilter(v)}>
          <ToggleButton value="all">All users</ToggleButton>
          <ToggleButton value="attention">Needs attention</ToggleButton>
        </ToggleButtonGroup>
      </Stack>

      <Paper variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell padding="checkbox" />
              <TableCell>User</TableCell><TableCell>Verdict</TableCell><TableCell>Parity</TableCell>
              <TableCell>Services</TableCell><TableCell>Last tallied</TableCell><TableCell padding="checkbox" />
            </TableRow>
          </TableHead>
          <TableBody>
            {users.map((u) => (
              <UserRow key={u.user} u={u} selected={picked.has(u.user)}
                       onSelect={(on) => setPicked((p) => { const n = new Set(p); on ? n.add(u.user) : n.delete(u.user); return n })} />
            ))}
            {users.length === 0 && (
              <TableRow><TableCell colSpan={7}>
                <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
                  {view ? (filter === 'attention' ? 'Nothing needs attention.' : 'No users in this migration yet.') : 'Loading…'}
                </Typography>
              </TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </Paper>

      <ReasonCodeDialog
        open={asking} busy={busy} title="Tally now"
        description={<>
          Counts every item of every service for {picked.size ? `${picked.size} selected user(s)` : 'every user'} on
          both tenants and compares the totals -- every item, never a sample. It writes nothing to either tenant.
          It runs as a job of its own (distinct from the whole-tenant tally behind Final Report&apos;s
          &quot;Run tally&quot;) and the result appears here.
        </>}
        onCancel={() => setAsking(false)} onConfirm={go} />
    </Box>
  )
}

export default Tally
