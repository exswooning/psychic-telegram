/**
 * The tenant's own one-to-one check, beside the per-user one: every source user has an
 * account, groups and their members, shared drives (members and every item), rooms, and
 * the per-user verdicts folded in. Read-only on both tenants (tenant_verify.py).
 */
import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, Collapse, IconButton, Paper, Stack, Table, TableBody, TableCell,
  TableRow, Typography,
} from '@mui/material'
import { KeyboardArrowDown as OpenIcon, KeyboardArrowUp as CloseIcon } from '@mui/icons-material'
import ReasonCodeDialog from './ReasonCodeDialog'
import { fetchTenantOneToOne, runTenantOneToOne } from '@/api/controlPlane'
import type { TenantReport, TenantSection } from '@/api/controlPlane'
import { VERDICT } from '@/oneToOneVerdicts'

const LABEL: Record<keyof TenantReport['sections'], string> = {
  users: 'Users have accounts', groups: 'Groups and members', sharedDrives: 'Shared drives',
  rooms: 'Rooms', perUser: 'Every user, one to one',
}

const Row: React.FC<{ k: keyof TenantReport['sections']; s: TenantSection }> = ({ k, s }) => {
  const [open, setOpen] = useState(false)
  const more = s.differenceCount > 0 || s.notes.length > 0 || !!s.error
  return (
    <>
      <TableRow data-testid={`tenant-section-${k}`}>
        <TableCell sx={{ fontWeight: 600 }}>{LABEL[k]}</TableCell>
        <TableCell><Chip size="small" color={VERDICT[s.status].color} label={VERDICT[s.status].label} /></TableCell>
        <TableCell sx={{ fontVariantNumeric: 'tabular-nums' }}>
          {s.error ? 'could not be checked' : s.compared ? `${s.matched} of ${s.compared} identical` : 'none on the source'}
        </TableCell>
        <TableCell padding="checkbox">
          {more && (
            <IconButton size="small" aria-label={`details of ${LABEL[k]}`} onClick={() => setOpen(!open)}>
              {open ? <CloseIcon fontSize="small" /> : <OpenIcon fontSize="small" />}
            </IconButton>
          )}
        </TableCell>
      </TableRow>
      {more && (
        <TableRow>
          <TableCell colSpan={4} sx={{ py: 0, borderBottom: open ? undefined : 'none' }}>
            <Collapse in={open} unmountOnExit>
              <Box sx={{ py: 1 }}>
                {s.error && <Alert severity="warning" sx={{ mb: 1 }}>{s.error}</Alert>}
                {s.notes.map((n) => <Typography key={n} variant="body2" color="text.secondary">{n}</Typography>)}
                {s.differences.map((d) => (
                  <Box key={d.item} sx={{ mt: 0.75 }}>
                    <Typography variant="body2" sx={{ fontWeight: 600, overflowWrap: 'anywhere' }}>{d.item}</Typography>
                    {d.diffs.map((x) => (
                      <Typography key={x} variant="caption" color="text.secondary"
                                  sx={{ display: 'block', overflowWrap: 'anywhere' }}>{x}</Typography>
                    ))}
                  </Box>
                ))}
                {s.differenceCount > s.differences.length && (
                  <Typography variant="caption" color="text.secondary">
                    and {s.differenceCount - s.differences.length} more
                  </Typography>
                )}
              </Box>
            </Collapse>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

const TenantOneToOne: React.FC<{ accountId?: number }> = ({ accountId }) => {
  const [report, setReport] = useState<TenantReport | null | undefined>(undefined)
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null)
  const load = useCallback(() => {
    if (!accountId) return
    fetchTenantOneToOne(accountId).then((r) => setReport(r.report)).catch(() => setReport(null))
  }, [accountId])
  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t) }, [load])

  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="tenant-one-to-one">
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }} flexWrap="wrap" useFlexGap>
        <Typography variant="h6" sx={{ fontWeight: 700 }}>Whole tenant</Typography>
        {report && <Chip color={VERDICT[report.verdict].color} label={VERDICT[report.verdict].label}
                         data-testid="tenant-verdict" />}
        {report === null && <Chip variant="outlined" label="Not checked yet" />}
        <Box sx={{ flexGrow: 1 }} />
        <Button size="small" variant="outlined" onClick={() => { setNote(null); setAsking(true) }}
                data-testid="tenant-verify-now">Check the tenant now</Button>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1, maxWidth: 760 }}>
        {report
          ? `${report.source} → ${report.target}, checked ${new Date(report.checkedAt).toLocaleString()}. `
          : ''}
        What belongs to the tenant rather than one user, opened on both sides: every user has an account, groups
        and who is in them, every shared drive with its members and every item in it, and rooms, plus the per-user
        verdicts below. Nothing is written to either tenant.
      </Typography>
      {note && <Alert severity={note.ok ? 'success' : 'error'} sx={{ mb: 1 }} onClose={() => setNote(null)}>{note.text}</Alert>}
      {report && (
        <Table size="small">
          <TableBody>
            {(Object.keys(LABEL) as (keyof TenantReport['sections'])[]).map((k) =>
              report.sections[k] ? <Row key={k} k={k} s={report.sections[k]} /> : null)}
          </TableBody>
        </Table>
      )}
      <ReasonCodeDialog
        open={asking} busy={busy} title="Check the tenant now"
        description="Opens both tenants and compares users, groups, shared drives item by item, and rooms. It writes nothing to either tenant, runs as a job of its own (watch it on Jobs), and the result appears here."
        onCancel={() => setAsking(false)}
        onConfirm={async (reason) => {
          setBusy(true)
          try {
            const r = await runTenantOneToOne(reason, accountId)
            setNote({ ok: r.ok, text: r.detail || (r.ok ? 'started' : 'refused') })
            if (r.ok) setAsking(false)
          } catch (e) {
            setNote({ ok: false, text: e instanceof Error ? e.message : String(e) })
          } finally { setBusy(false) }
        }} />
    </Paper>
  )
}

export default TenantOneToOne
