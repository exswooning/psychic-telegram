import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, Box, Button, Chip, CircularProgress, Paper, Stack, Table,
  TableBody, TableCell, TableContainer, TableHead, TableRow, Tooltip,
  Typography,
} from '@mui/material'
import { FactCheck as CoverageIcon } from '@mui/icons-material'
import {
  CoverageStatus, startCoverage, fetchCoverageStatus, getOperator,
} from '@/api/controlPlane'
import ReasonCodeDialog from './ReasonCodeDialog'

/**
 * "Which supported data types does the source actually have?"
 *
 * scope.py says what the engine can migrate; this is the live join against
 * what the tenant actually contains. The distinction that matters is
 * ABSENT vs UNPROBED: a category with zero source instances will report a
 * clean migration without the code path ever running, and a category this
 * process could not measure (a scope not yet granted) must never be
 * confused with "definitely empty" -- that is the same failure shape as a
 * benchmark judge passing a run that copied nothing. See coverage_audit.py.
 *
 * Runs detached, like a benchmark: it makes one real API call per user per
 * service, which is minutes, not seconds.
 */
const CoverageAudit: React.FC = () => {
  const [status, setStatus] = useState<CoverageStatus | null>(null)
  const [ask, setAsk] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const poll = useCallback(() => {
    fetchCoverageStatus().then(setStatus).catch(() => {})
  }, [])

  useEffect(() => {
    poll()
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [poll])

  useEffect(() => {
    if (status?.running && !pollRef.current) {
      pollRef.current = setInterval(poll, 5000)
    } else if (!status?.running && pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [status?.running, poll])

  const launch = async (reason: string) => {
    setBusy(true); setError(null)
    try {
      await startCoverage(reason)
      setAsk(false)
      poll()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const isAdmin = !!getOperator()
  const result = status?.result

  return (
    <Paper variant="outlined" sx={{ borderRadius: 2, p: 2 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1, flexWrap: 'wrap', gap: 1 }}>
        <CoverageIcon color="action" />
        <Typography variant="h6" sx={{ flexGrow: 1 }}>Source coverage</Typography>
        {status?.running && (
          <Chip size="small" color="primary" icon={<CircularProgress size={10} />}
                label="scanning…" />
        )}
        <Button variant="outlined" size="small" disabled={!isAdmin || status?.running}
                onClick={() => setAsk(true)}>
          {status?.running ? 'Running…' : 'Run audit'}
        </Button>
      </Stack>

      <Typography variant="caption" color="text.secondary">
        Which supported data types this tenant actually has an instance of.
        A category with zero instances reports a clean migration without the
        code that handles it ever running.
      </Typography>

      {!isAdmin && (
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
          Set an operator name to run it.
        </Typography>
      )}
      {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}

      {result && (
        <Box sx={{ mt: 2 }}>
          <Stack direction="row" spacing={2} sx={{ mb: 1.5, flexWrap: 'wrap', gap: 1 }}>
            <Chip size="small" color="success" variant="outlined"
                  label={`${result.counts.covered} covered`} />
            <Chip size="small" color="error" variant="outlined"
                  label={`${result.counts.absent} absent`} />
            <Chip size="small" color="default" variant="outlined"
                  label={`${result.counts.unprobed} unprobed`} />
          </Stack>

          {result.externalSharedWithMe > 0 && !result.migrateExternalShares && (
            <Alert severity="error" sx={{ mb: 2 }}>
              <strong>Data loss risk:</strong> {result.externalSharedWithMe} file(s)
              are shared into these users by owners outside the org. Nobody
              inside the tenant owns them, so no user's migration carries
              them, and MIGRATE_EXTERNAL_SHARES is off -- they will be
              dropped, not deferred.
            </Alert>
          )}

          {Object.keys(result.errors).length > 0 && (
            <Alert severity="warning" sx={{ mb: 2 }}>
              Could not scan: {Object.entries(result.errors).map(([u, e]) =>
                `${u} (${e})`).join(', ')}. Their data is unmeasured, not absent.
            </Alert>
          )}

          <TableContainer sx={{ maxHeight: 420, overflowX: 'auto' }}>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell>Service</TableCell>
                  <TableCell>Item</TableCell>
                  <TableCell>Verdict</TableCell>
                  <TableCell align="right">Count</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {result.rows
                  .filter((r) => r.verdict !== 'N/A')
                  .sort((a, b) => {
                    const order: Record<string, number> = { ABSENT: 0, UNPROBED: 1, COVERED: 2 }
                    return (order[a.verdict] ?? 3) - (order[b.verdict] ?? 3)
                  })
                  .map((r) => (
                    <TableRow key={`${r.service}:${r.item}`}>
                      <TableCell>{r.service}</TableCell>
                      <TableCell>
                        <Tooltip title={r.note}>
                          <span>{r.item}</span>
                        </Tooltip>
                      </TableCell>
                      <TableCell>
                        <Chip size="small"
                              label={r.verdict}
                              color={r.verdict === 'COVERED' ? 'success'
                                    : r.verdict === 'ABSENT' ? 'error' : 'default'}
                              variant={r.verdict === 'COVERED' ? 'outlined' : 'filled'} />
                      </TableCell>
                      <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                        {r.count ?? '—'}
                      </TableCell>
                    </TableRow>
                  ))}
              </TableBody>
            </Table>
          </TableContainer>
        </Box>
      )}

      {!result && !status?.running && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
          No audit has run yet in this checkout.
        </Typography>
      )}

      <ReasonCodeDialog
        open={ask} busy={busy} error={error}
        title="Run source coverage audit"
        description={
          <>
            Read-only against Drive, Gmail, Calendar, Chat, People and Tasks
            for every mapped source user -- one real API call per user per
            service, so this takes minutes. Writes nothing to either tenant.
          </>
        }
        onCancel={() => { setAsk(false); setError(null) }}
        onConfirm={launch}
      />
    </Paper>
  )
}

export default CoverageAudit
