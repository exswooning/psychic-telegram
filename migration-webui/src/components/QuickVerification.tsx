/**
 * What a quick migration found when it checked its own work.
 *
 * A quick migration copies a small slice and then, on the server, compares every
 * copied item with its original: Drive files are downloaded from both sides and
 * hashed, messages are compared byte for byte, and so on (verify_sample.py). The
 * result is saved there, so it is here whether or not anyone was watching when the
 * run ended.
 *
 * IDENTICAL means every paired item matched and nothing was left over. INCOMPLETE
 * means some check could not be made, and is shown in amber, never green: a check
 * that was not made is not a pass -- the rule the run reports follow too.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { Alert, Box, Button, Chip, Paper, Stack, Table, TableBody, TableCell, TableHead, TableRow, Typography } from '@mui/material'
import { fetchQuickLatest, quickReportUrl } from '@/api/controlPlane'
import type { QuickReport, QuickVerdict } from '@/api/controlPlane'

const VERDICT: Record<QuickVerdict, { color: 'success' | 'error' | 'warning'; hint: string }> = {
  IDENTICAL: { color: 'success', hint: 'Every copied item matched its original, and nothing was left over.' },
  DIFFERENCES: { color: 'error', hint: 'Something copied does not match its original, is missing, or was copied twice.' },
  INCOMPLETE: { color: 'warning', hint: 'Some check could not be made. That is not a pass.' },
}

const when = (iso: string) => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

export const QuickVerification: React.FC<{
  accountId: number
  /** Changes when a run starts or ends, so a fresh result is fetched. */
  refreshKey?: unknown
}> = ({ accountId, refreshKey }) => {
  const [report, setReport] = useState<QuickReport | null | undefined>(undefined)
  const [error, setError] = useState('')
  const load = useCallback(() => {
    fetchQuickLatest(accountId)
      .then((r) => { setReport(r); setError('') })
      .catch((e) => { setReport((v) => v ?? null); setError(e instanceof Error ? e.message : String(e)) })
  }, [accountId])
  useEffect(load, [load, refreshKey])

  if (report === undefined || (report === null && !error)) return null       // nothing to say yet
  if (!report) return <Alert severity="warning" sx={{ mb: 2 }}>{error}</Alert>
  const t = report.totals
  const v = VERDICT[report.verdict]
  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="quick-verification">
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <Typography variant="h6" sx={{ fontWeight: 700, flexGrow: 1 }}>Quick migration check</Typography>
        <Chip label={report.verdict} color={v.color} size="small" sx={{ fontWeight: 700 }} data-testid="quick-verdict" />
        <Button size="small" variant="outlined" component="a" href={quickReportUrl(accountId)} download
                data-testid="quick-download">Download report</Button>
      </Stack>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
        {when(report.generatedAt)} · {report.sourceDomain} → {report.targetDomain}
        {report.sampleLimit ? ` · a sample of ${report.sampleLimit} per service` : ''} · {v.hint}
      </Typography>
      <Typography variant="body2" data-testid="quick-summary">
        <strong>{t.identical.toLocaleString()} of {t.checked.toLocaleString()}</strong> copied items are identical to
        their originals. {t.filesOpened.toLocaleString()} Drive files were downloaded from both sides and compared
        ({t.bytesCompared.toLocaleString()} bytes).
        {t.differences > 0 && ` ${t.differences} differ.`}
        {t.missing > 0 && ` ${t.missing} missing on the target.`}
        {t.duplicates > 0 && ` ${t.duplicates} copied twice.`}
        {t.notCopied > 0 && ` ${t.notCopied} failed to copy.`}
        {t.errors > 0 && ` ${t.errors} check(s) could not be made.`}
        {t.extras > 0 && ` ${t.extras} other item(s) on the target that this migration did not create.`}
      </Typography>
      {report.reasons.length > 0 && (
        <Box component="ul" sx={{ m: 0, mt: 1, pl: 2.5, fontSize: 13 }} data-testid="quick-reasons">
          {report.reasons.map((r) => <li key={r}>{r}</li>)}
        </Box>
      )}
      <Table size="small" sx={{ mt: 1.5 }} data-testid="quick-table">
        <TableHead>
          <TableRow>
            <TableCell>user</TableCell>
            {report.services.map((s) => <TableCell key={s}>{s}</TableCell>)}
          </TableRow>
        </TableHead>
        <TableBody>
          {Object.entries(report.users).map(([user, per]) => (
            <TableRow key={user}>
              <TableCell>{user.split('@')[0]}</TableCell>
              {report.services.map((s) => {
                const r = per[s]
                if (!r) return <TableCell key={s}>not checked</TableCell>
                const bad = r.differences.length + r.missing.length + r.duplicates.length + r.notCopied.length + r.errors.length
                return (
                  <TableCell key={s} data-testid={`quick-cell-${user.split('@')[0]}-${s}`}>
                    {r.identical}/{r.checked}{bad > 0 ? ` · ${bad} problem${bad === 1 ? '' : 's'}` : ''}
                  </TableCell>
                )
              })}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Paper>
  )
}

export default QuickVerification
