/**
 * One-to-one check: is what landed on the target what was on the source?
 *
 * Each user is compared against both tenants as their migration finishes (verify_sample.py,
 * started from main.migrate_user): Drive files are downloaded or exported from both sides and
 * hashed, mail is compared as raw messages, events, contacts and tasks field by field. The
 * result is kept per user and service, so it is here whether or not anyone was watching.
 *
 * Two rules the page keeps to. A user nobody has checked is NOT VERIFIED, in grey -- never a
 * blank that reads as fine. And INCOMPLETE (some check could not be made) is amber, never
 * green: a check that was not made is not a pass. A checked sample says how much of the user
 * it was ("25 of 4,180"), so IDENTICAL over a slice is never read as IDENTICAL over the whole.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Alert, Box, Button, Checkbox, Chip, Collapse, FormControlLabel, IconButton, Paper, Stack, Table,
  TableBody, TableCell, TableHead, TableRow, ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from '@mui/material'
import { KeyboardArrowDown as OpenIcon, KeyboardArrowUp as CloseIcon, Refresh as RefreshIcon } from '@mui/icons-material'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'
import { fetchMe, fetchOneToOne, runOneToOne } from '@/api/controlPlane'
import type { OneToOneService, OneToOneUser, OneToOneVerdict, OneToOneView } from '@/api/controlPlane'

// Shared with MigrationDetail's compact summary section, from its own module (not
// exported from here) so this page file keeps exporting only its component.
import { VERDICT, ORDER } from '@/oneToOneVerdicts'

const when = (iso: string | null) => {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

const ServiceChip: React.FC<{ s: OneToOneService }> = ({ s }) => {
  const v = VERDICT[s.verdict]
  const of = s.sampledOf && s.sampledOf > s.checked ? ` of ${s.sampledOf.toLocaleString()}` : ''
  return (
    <Tooltip title={`${v.hint} Checked ${s.checked.toLocaleString()}${of}; ${s.identical.toLocaleString()} identical.`}>
      <Chip size="small" color={v.color} variant={s.verdict === 'IDENTICAL' ? 'outlined' : 'filled'}
            label={`${s.service} ${s.identical}/${s.checked}${of}`} data-testid={`svc-${s.service}`} />
    </Tooltip>
  )
}

const Findings: React.FC<{ s: OneToOneService }> = ({ s }) => {
  const rows: string[] = []
  for (const d of s.differences ?? []) rows.push(`${d.path ?? d.item ?? 'item'}: ${(d.diffs ?? []).slice(0, 3).join(' · ')}`)
  for (const m of s.missing ?? []) rows.push(`missing — ${m.name ?? ''} ${m.why ?? ''}`)
  for (const n of s.notCopied ?? []) rows.push(`failed to copy — ${n.error ?? n.id ?? ''}`)
  for (const e of s.errors ?? []) rows.push(`could not check — ${e}`)
  for (const d of s.duplicates ?? []) rows.push(`copied twice — ${d.name ?? d.messageId ?? d.path ?? ''}`)
  // Only the kinds listed above: strays are informational and are not among them, so counting
  // them made the page promise "9 more" of things it had no way to show.
  const c = s.counts ?? {}
  const counted = (c.differences ?? 0) + (c.missing ?? 0) + (c.notCopied ?? 0) + (c.errors ?? 0) + (c.duplicates ?? 0)
  const shown = Math.min(rows.length, 8)
  return (
    <Box sx={{ mb: 1.5 }}>
      <Typography variant="subtitle2">{s.service} · {VERDICT[s.verdict].label} · {when(s.verifiedAt)}</Typography>
      {rows.slice(0, 8).map((r, i) => (
        <Typography key={i} variant="body2" sx={{ fontFamily: 'ui-monospace, monospace', fontSize: 12, wordBreak: 'break-word' }}>{r}</Typography>
      ))}
      {counted > shown && (
        <Typography variant="caption" color="text.secondary">
          … {counted - shown} more; run "Verify now" on this user for the full report.
        </Typography>
      )}
      {(s.notes ?? []).map((n, i) => <Typography key={i} variant="caption" color="text.secondary" sx={{ display: 'block' }}>{n}</Typography>)}
    </Box>
  )
}

const UserRow: React.FC<{ u: OneToOneUser; selected: boolean; onSelect: (on: boolean) => void }> = ({ u, selected, onSelect }) => {
  const [open, setOpen] = useState(false)
  const v = VERDICT[u.verdict]
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
        <TableCell>
          <Stack direction="row" flexWrap="wrap" gap={0.5}>
            {u.services.length ? u.services.map((s) => <ServiceChip key={s.service} s={s} />)
              : <Typography variant="caption" color="text.secondary">{u.status === 'DONE' ? 'finished — not checked yet' : `status ${u.status ?? 'unknown'}`}</Typography>}
          </Stack>
        </TableCell>
        <TableCell><Typography variant="caption">{when(u.verifiedAt)}</Typography></TableCell>
        <TableCell padding="checkbox">
          {u.services.length > 0 && (
            <IconButton size="small" onClick={() => setOpen((o) => !o)} aria-label={open ? `hide ${u.user}` : `show ${u.user}`}>
              {open ? <CloseIcon fontSize="small" /> : <OpenIcon fontSize="small" />}
            </IconButton>
          )}
        </TableCell>
      </TableRow>
      {u.services.length > 0 && (
        <TableRow>
          <TableCell colSpan={6} sx={{ py: 0, borderBottom: open ? undefined : 'none' }}>
            <Collapse in={open} unmountOnExit><Box sx={{ py: 1.5, pl: 2 }}>{u.services.map((s) => <Findings key={s.service} s={s} />)}</Box></Collapse>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

// What "every item" means for these users, from each one's last check: every service's whole
// item count against the slice a sample opens. A user nobody has checked has no count yet.
const sizeOf = (us: OneToOneUser[], per: number) => {
  let all = 0, sample = 0, unknown = 0
  for (const u of us) {
    if (!u.services.length) { unknown += 1; continue }
    for (const s of u.services) {
      const n = s.sampledOf ?? s.checked
      all += n
      sample += Math.min(n, per)
    }
  }
  return { all, sample, unknown }
}

const OneToOne: React.FC = () => {
  const [params] = useSearchParams()
  const asked = Number(params.get('account')) || undefined
  const [accountId, setAccountId] = useState<number | undefined>(asked)
  const [view, setView] = useState<OneToOneView | null>(null)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState<'attention' | 'all'>('all')
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [asking, setAsking] = useState(false)
  const [everything, setEverything] = useState(false)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null)

  useEffect(() => {
    if (accountId) return
    fetchMe().then((m) => setAccountId(m.id)).catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [accountId])

  const load = useCallback(() => {
    if (!accountId) return
    fetchOneToOne(accountId).then((v) => { setView(v); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [accountId])
  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t) }, [load])

  const users = useMemo(() => {
    const all = [...(view?.users ?? [])].sort((a, b) => ORDER.indexOf(a.verdict) - ORDER.indexOf(b.verdict) || a.user.localeCompare(b.user))
    return filter === 'all' ? all : all.filter((u) => u.verdict !== 'IDENTICAL')
  }, [view, filter])

  const totals = view?.totals ?? {}
  const size = sizeOf((view?.users ?? []).filter((u) => !picked.size || picked.has(u.user)), view?.perService ?? 25)
  const go = async (reason: string) => {
    setBusy(true)
    try {
      const r = await runOneToOne(reason, { accountId, users: [...picked], limit: everything ? 0 : undefined })
      setNote({ ok: r.ok, text: r.detail || (r.ok ? 'started' : 'refused') })
      if (r.ok) setAsking(false)
    } catch (e) {
      setNote({ ok: false, text: e instanceof Error ? e.message : String(e) })
    } finally { setBusy(false) }
  }

  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <Typography variant="h5" sx={{ fontWeight: 700, flexGrow: 1 }}>One-to-one check</Typography>
        <Button size="small" startIcon={<RefreshIcon />} onClick={load}>Refresh</Button>
        <Button size="small" variant="contained" onClick={() => { setNote(null); setAsking(true) }} data-testid="verify-now">
          {picked.size ? `Verify ${picked.size} selected` : 'Verify all now'}
        </Button>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 760 }}>
        {view?.onComplete === false
          ? 'Automatic checking is switched off for this account (VERIFY_ON_COMPLETE=0); use Verify now.'
          : `Each user is compared against both tenants the moment their migration finishes: a sample of up to ${view?.perService ?? 25} of each kind of item, opened on both sides. Nothing is written to either tenant.`}
      </Typography>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {note && <Alert severity={note.ok ? 'success' : 'error'} sx={{ mb: 2 }} onClose={() => setNote(null)}>{note.text}</Alert>}

      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
        {ORDER.map((k) => (
          <Chip key={k} color={VERDICT[k].color} variant={(totals[k] ?? 0) ? 'filled' : 'outlined'}
                label={`${totals[k] ?? 0} ${VERDICT[k].label.toLowerCase()}`} data-testid={`total-${k}`} />
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
              <TableCell>User</TableCell><TableCell>Verdict</TableCell><TableCell>Services checked</TableCell>
              <TableCell>Last checked</TableCell><TableCell padding="checkbox" />
            </TableRow>
          </TableHead>
          <TableBody>
            {users.map((u) => (
              <UserRow key={u.user} u={u} selected={picked.has(u.user)}
                       onSelect={(on) => setPicked((p) => { const n = new Set(p); on ? n.add(u.user) : n.delete(u.user); return n })} />
            ))}
            {users.length === 0 && (
              <TableRow><TableCell colSpan={6}>
                <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
                  {view ? (filter === 'attention' ? 'Nothing needs attention.' : 'No users in this migration yet.') : 'Loading…'}
                </Typography>
              </TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </Paper>

      <ReasonCodeDialog
        open={asking} busy={busy} title="Verify now"
        description={<>
          Opens {picked.size ? `${picked.size} selected user(s)` : 'every user'} on both tenants and compares them. It writes
          nothing to either tenant. It runs as a job of its own and the result appears here.
          <FormControlLabel sx={{ display: 'block', mt: 1 }} control={
            <Checkbox size="small" checked={everything} onChange={(e) => setEverything(e.target.checked)} inputProps={{ 'aria-label': 'check every item' }} />}
            label="Check every item, not a sample (slow on a large user)" />
          {everything && (
            <Typography variant="body2" color="text.secondary" data-testid="every-item-size">
              {size.sample
                ? `About ${size.all.toLocaleString()} items, ${Math.max(1, Math.round(size.all / size.sample))}× the ${size.sample.toLocaleString()} a sample opens: expect it to take about that many times as long.`
                : 'Nobody chosen has been checked yet, so how many items they hold is not known.'}
              {size.sample > 0 && size.unknown > 0 && ` ${size.unknown} never checked ${size.unknown === 1 ? 'is' : 'are'} not counted.`}
            </Typography>
          )}
        </>}
        onCancel={() => setAsking(false)} onConfirm={go} />
    </Box>
  )
}

export default OneToOne
