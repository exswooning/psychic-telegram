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
  Alert, Box, Button, Chip, CircularProgress, Paper, Stack, Tooltip, Typography,
} from '@mui/material'
import {
  Description as JsonIcon, PictureAsPdf as PdfIcon, SmartToy as ClaudeIcon,
} from '@mui/icons-material'
import { fetchReports, generateReport, reportUrl } from '@/api/controlPlane'
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

  const load = useCallback(() => {
    fetchReports(accountId)
      .then((r) => { setReports(r.reports); setError(r.error) })
      .catch((e) => { setReports((v) => v ?? []); setError(e instanceof Error ? e.message : String(e)) })
  }, [accountId])
  useEffect(load, [load])

  const make = async () => {
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

  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="run-reports">
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <Typography variant="h6" sx={{ fontWeight: 700, flexGrow: 1 }}>Run reports</Typography>
        <Button variant="contained" size="small" onClick={make} disabled={busy}
                startIcon={busy ? <CircularProgress size={14} /> : undefined}>
          {busy ? 'Generating…' : 'Generate report now'}
        </Button>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5, maxWidth: 780 }}>
        Each report is judged against benchmarks and saved, so it is still here after a restart.
        Download the <strong>human</strong> PDF to read, or the <strong>Claude</strong> PDF to hand
        to Claude Code — it carries the failing checks with exact values, the error families, the
        configuration and the tail of the run&apos;s log.
      </Typography>

      {error && <Alert severity="warning" sx={{ mb: 1.5 }}>{error}</Alert>}

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
            </Box>
            <Stack direction="row" spacing={0.75}>
              <Button size="small" variant="outlined" startIcon={<PdfIcon />} component="a"
                      href={reportUrl(r.id, 'human', r.accountId ?? accountId)} download disabled={!r.files.includes('human.pdf')}
                      data-testid={`pdf-human-${r.id}`}>Human PDF</Button>
              <Button size="small" variant="outlined" startIcon={<ClaudeIcon />} component="a"
                      href={reportUrl(r.id, 'claude', r.accountId ?? accountId)} download disabled={!r.files.includes('claude.pdf')}
                      data-testid={`pdf-claude-${r.id}`}>Claude PDF</Button>
              <Button size="small" startIcon={<JsonIcon />} component="a"
                      href={reportUrl(r.id, 'json', r.accountId ?? accountId)} target="_blank" rel="noreferrer">JSON</Button>
            </Stack>
          </Stack>
        ))}
      </Stack>
    </Paper>
  )
}

export default RunReports
