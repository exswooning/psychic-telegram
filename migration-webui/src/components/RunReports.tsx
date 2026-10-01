/**
 * Every saved run report, always reachable, with the two PDFs.
 *
 * A report is judged against benchmarks (benchmarks.py), and the verdict has
 * three values on purpose. PASS means every required check was made and none
 * failed. FAIL means one did. UNVERIFIED means a required check could not be
 * made -- typically that nobody compared the two tenants -- and it is shown
 * in amber, never green: a check that was not made is not a pass.
 *
 * Reports live on disk, so this panel works when nothing is running, after a
 * restart, and on a tenant that has never had a job.
 */
import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, CircularProgress, Collapse, IconButton, Paper, Stack, Tooltip, Typography,
} from '@mui/material'
import {
  Download as DownloadIcon, ExpandLess as CloseIcon, ExpandMore as OpenIcon,
} from '@mui/icons-material'
import { fetchReports, generateReport, reportUrl, startTally } from '@/api/controlPlane'
import type { ReportSummary, Verdict } from '@/api/controlPlane'

const VERDICT: Record<Verdict, { color: 'success' | 'error' | 'warning'; hint: string }> = {
  PASS: { color: 'success', hint: 'Every required check was made and none failed.' },
  FAIL: { color: 'error', hint: 'At least one benchmark failed.' },
  UNVERIFIED: { color: 'warning',
    hint: 'No benchmark failed, but a required check could not be made (usually: the two tenants were never compared). That is not a pass.' },
}

const when = (iso: string) => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

export const RunReports: React.FC<{
  /** Show this account's reports and generate for it. Omitted, the caller's
   *  own -- or every account's, for a superadmin. */
  accountId?: number
}> = ({ accountId }) => {
  const [reports, setReports] = useState<ReportSummary[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [tally, setTally] = useState('')
  // Collapsed unless this viewer left it open: every report ever made sat above the
  // rest of the migration page. Remembered per browser only -- a convenience.
  const [open, setOpen] = useState<boolean>(() => {
    try { return localStorage.getItem('runReports.open') === '1' } catch { return false }
  })
  const toggle = () => setOpen((o) => {
    try { localStorage.setItem('runReports.open', o ? '0' : '1') } catch { /* storage blocked */ }
    return !o
  })

  const load = useCallback(() => {
    fetchReports(accountId)
      .then((r) => { setReports(r.reports); setError(r.error) })
      .catch((e) => { setReports((v) => v ?? []); setError(e instanceof Error ? e.message : String(e)) })
  }, [accountId])
  useEffect(load, [load])

  const make = async () => {
    setOpen(true)           // the new report, or why it failed, has to be visible
    setBusy(true)
    setError('')
    try {
      await generateReport(accountId)
      load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const runTally = async () => {
    setOpen(true)
    setTally('')
    setError('')
    try {
      const r = await startTally(accountId)
      setTally(r.detail || 'Tally started.')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const latest = (reports ?? [])[0]
  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="run-reports">
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: open ? 0.5 : 0 }}>
        <IconButton size="small" onClick={toggle} aria-expanded={open}
                    aria-label={open ? 'hide run reports' : 'show run reports'} data-testid="reports-toggle">
          {open ? <CloseIcon fontSize="small" /> : <OpenIcon fontSize="small" />}
        </IconButton>
        <Typography variant="h6" sx={{ fontWeight: 700, cursor: 'pointer' }} onClick={toggle}>
          Run reports
        </Typography>
        {/* Closed, the card still says what matters: how many, and the newest verdict. */}
        <Stack direction="row" spacing={1} alignItems="center" sx={{ flexGrow: 1 }} data-testid="reports-summary">
          {reports !== null && (
            <Typography variant="body2" color="text.secondary">
              {reports.length} report{reports.length === 1 ? '' : 's'}
            </Typography>
          )}
          {latest && (
            <Chip size="small" label={`latest ${latest.verdict}`} color={VERDICT[latest.verdict].color}
                  sx={{ fontWeight: 700 }} />
          )}
        </Stack>
        <Button variant="outlined" size="small" onClick={runTally}>Run tally</Button>
        <Button variant="contained" size="small" onClick={make} disabled={busy}
                startIcon={busy ? <CircularProgress size={14} /> : undefined}>
          {busy ? 'Generating…' : 'Generate report now'}
        </Button>
      </Stack>
      <Collapse in={open} unmountOnExit>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5, maxWidth: 780 }}>
        Each report is judged against benchmarks and saved, so it is still here after a restart.
      </Typography>

      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5, maxWidth: 780 }}>
        <strong>Run tally</strong> counts what both tenants actually hold — Drive, mail, calendar,
        contacts, tasks — and spot-checks a sample for byte-level checksums, modified times and
        sharing. Read-only. Until one has run, a report cannot say the tenants agree, so it reads
        UNVERIFIED. Run a tally, then generate the report.
      </Typography>

      {error && <Alert severity="warning" sx={{ mb: 1.5 }}>{error}</Alert>}
      {tally && <Alert severity="info" sx={{ mb: 1.5 }} data-testid="tally-started">{tally}</Alert>}

      {reports === null && <CircularProgress size={18} />}
      {reports !== null && reports.length === 0 && !error && (
        <Typography variant="body2" color="text.secondary" data-testid="no-reports">
          No reports yet. Press <em>Generate report now</em> to build one from the ledger as it stands.
        </Typography>
      )}

      <Stack spacing={1}>
        {(reports ?? []).map((r) => (
          <Stack key={r.id} direction={{ xs: 'column', md: 'row' }} spacing={1.5} alignItems={{ md: 'center' }}
                 data-testid={`report-${r.id}`}
                 sx={{ p: 1.25, border: '1px solid', borderColor: 'divider', borderRadius: 1 }}>
            <Tooltip title={VERDICT[r.verdict].hint}>
              <Chip label={r.verdict} color={VERDICT[r.verdict].color} size="small"
                    sx={{ fontWeight: 700, minWidth: 96 }} data-testid={`verdict-${r.id}`} />
            </Tooltip>
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                {r.kind} · {when(r.generatedAt)}
                {r.tenants?.source && ` · ${r.tenants.source} → ${r.tenants.target ?? '?'}`}
                {/* Only when looking across accounts: on one account's own
                    page it would just repeat the header. */}
                {!accountId && r.accountId ? ` · account #${r.accountId}` : ''}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {r.counts.pass} passed · {r.counts.warn} warning{r.counts.warn === 1 ? '' : 's'} ·{' '}
                {r.counts.fail} failed · {r.counts.unknown} not checked
                {r.returnCode != null && ` · exit ${r.returnCode}`}
              </Typography>
              {/* Per-user, not the tenant-wide fidelity above: what verify_sample found for
                  each user, same rollup as the One-to-one page. Absent for a seed report. */}
              {r.oneToOne && (
                <Typography variant="caption" color="text.secondary" component="div" data-testid={`one-to-one-${r.id}`}>
                  One-to-one: {r.oneToOne.IDENTICAL ?? 0} identical
                  {(r.oneToOne.DIFFERENCES ?? 0) > 0 && `, ${r.oneToOne.DIFFERENCES} with differences`}
                  {(r.oneToOne.INCOMPLETE ?? 0) > 0 && `, ${r.oneToOne.INCOMPLETE} incomplete`}
                  {(r.oneToOne.NOT_VERIFIED ?? 0) > 0 && `, ${r.oneToOne.NOT_VERIFIED} not verified`}
                </Typography>
              )}
            </Box>
            {/* One download. The two PDFs read the same to the people using them, so
                offering both was a choice nobody could make; the second is the fallback
                for a report that only has that one. */}
            {(() => {
              const kind = r.files.includes('human.pdf') ? 'human'
                : r.files.includes('claude.pdf') ? 'claude' : null
              return (
                <span>
                  <Button size="small" variant="outlined" startIcon={<DownloadIcon />} component="a"
                          href={kind ? reportUrl(r.id, kind, r.accountId ?? accountId) : undefined}
                          download disabled={!kind} data-testid={`download-${r.id}`}>
                    Download report
                  </Button>
                </span>
              )
            })()}
          </Stack>
        ))}
      </Stack>
      </Collapse>
    </Paper>
  )
}

export default RunReports
