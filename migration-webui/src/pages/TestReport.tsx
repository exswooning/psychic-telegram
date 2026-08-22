import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, CircularProgress, IconButton, LinearProgress,
  Paper, Stack, Table, TableBody, TableCell, TableHead, TableRow, Typography,
} from '@mui/material'
import {
  Refresh as RefreshIcon, PlayArrow as RunIcon,
} from '@mui/icons-material'
import { fetchTestReport, runTests, TestReport as Report } from '@/api/controlPlane'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'

/**
 * The suite is the only evidence that this tool behaves as described.
 *
 * It used to exist solely as scrollback in whoever's terminal last ran it, so
 * an operator deciding whether to trust a migration could not see whether the
 * thing had been verified at all, let alone when or against which commit.
 *
 * Failures first and in full: a green summary with the failures folded away
 * is how people learn to skim the one screen that is meant to stop them.
 */

export const TestReport: React.FC = () => {
  const [r, setR] = useState<Report | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [ask, setAsk] = useState(false)
  const [busy, setBusy] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  const [started, setStarted] = useState('')

  const refresh = useCallback(() => {
    setLoading(true)
    fetchTestReport()
      .then((x) => { setR(x); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    refresh()
    // Polled while a run is in flight; the suite takes about three minutes.
    const t = window.setInterval(refresh, 10_000)
    return () => window.clearInterval(t)
  }, [refresh])

  return (
    <Box sx={{ p: 3 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 2 }}>
        <Typography variant="h5" sx={{ fontWeight: 700 }}>Test suite</Typography>
        {loading && <CircularProgress size={16} />}
        <Box sx={{ flex: 1 }} />
        <Button size="small" variant="outlined" startIcon={<RunIcon />}
                data-testid="run-tests" disabled={busy || r?.running}
                onClick={() => setAsk(true)}>
          {r?.running ? 'running…' : 'Run suite'}
        </Button>
        <IconButton size="small" onClick={refresh} aria-label="refresh">
          <RefreshIcon fontSize="small" />
        </IconButton>
      </Stack>

      {started && (
        <Alert severity="success" sx={{ mb: 2 }} data-testid="run-started">
          {started}
        </Alert>
      )}
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {r?.running && <LinearProgress sx={{ mb: 2 }} data-testid="running-bar" />}

      {r?.neverRun ? (
        <Alert severity="info" data-testid="never-run">
          {r.detail || 'the suite has not been run on this host yet'}
        </Alert>
      ) : r ? (
        <>
          <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1.5 }}>
              <Chip data-testid="suite-verdict"
                    color={r.ok ? 'success' : 'error'}
                    variant={r.ok ? 'outlined' : 'filled'}
                    label={r.ok ? 'passing' : `${r.failed} failing`} />
              {r.commit && (
                <Chip size="small" variant="outlined"
                      label={`commit ${r.commit}`} data-testid="suite-commit" />
              )}
              <Typography variant="caption" color="text.secondary">
                {r.ranAt ? `ran ${r.ranAt}` : ''}
                {r.wallSec ? ` · ${r.wallSec}s` : ''}
              </Typography>
            </Stack>
            <Stack direction="row" spacing={4} sx={{ flexWrap: 'wrap', gap: 2 }}>
              {([
                ['total', 'tests', r.total, undefined],
                ['passed', 'passed', r.passed, undefined],
                ['failed', 'failed', r.failed, 'error'],
                ['skipped', 'skipped', r.skipped, undefined],
              ] as const).map(([id, label, value, tone]) => (
                <Box key={id} data-testid={`tests-${id}`}>
                  <Typography sx={{
                    fontWeight: 700, fontSize: 22, lineHeight: 1.2,
                    fontVariantNumeric: 'tabular-nums',
                    color: tone === 'error' && value > 0 ? 'error.main' : 'text.primary',
                  }}>
                    {value.toLocaleString()}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">{label}</Typography>
                </Box>
              ))}
            </Stack>
          </Paper>

          {r.failures.length > 0 && (
            <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="failures">
              <Typography variant="subtitle2"
                          sx={{ fontWeight: 700, mb: 1, color: 'error.main' }}>
                Failures ({r.failures.length})
              </Typography>
              <Stack spacing={2}>
                {r.failures.map((f) => (
                  <Box key={f.name} data-testid={`failure-${f.name}`}>
                    <Typography variant="body2" sx={{ fontWeight: 600 }}>
                      {f.name}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {f.message}
                    </Typography>
                    {f.detail && (
                      <Box component="pre" sx={{
                        mt: 0.5, p: 1, fontSize: 11, lineHeight: 1.5,
                        bgcolor: 'action.hover', borderRadius: 1,
                        overflowX: 'auto', maxHeight: 220,
                      }}>
                        {f.detail}
                      </Box>
                    )}
                  </Box>
                ))}
              </Stack>
            </Paper>
          )}

          <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="by-file">
            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
              By file ({r.files.length})
            </Typography>
            <Box sx={{ maxHeight: 420, overflowY: 'auto' }}>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    <TableCell>file</TableCell>
                    <TableCell align="right">passed</TableCell>
                    <TableCell align="right">failed</TableCell>
                    <TableCell align="right">skipped</TableCell>
                    <TableCell align="right">time</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {r.files.map((f) => (
                    <TableRow key={f.file} data-testid={`file-${f.file}`}>
                      <TableCell sx={{ fontSize: 12 }}>{f.file}</TableCell>
                      <TableCell align="right"
                                 sx={{ fontVariantNumeric: 'tabular-nums' }}>
                        {f.passed}
                      </TableCell>
                      <TableCell align="right"
                                 sx={{ fontVariantNumeric: 'tabular-nums',
                                       color: f.failed ? 'error.main' : undefined,
                                       fontWeight: f.failed ? 700 : 400 }}>
                        {f.failed}
                      </TableCell>
                      <TableCell align="right"
                                 sx={{ fontVariantNumeric: 'tabular-nums' }}>
                        {f.skipped}
                      </TableCell>
                      <TableCell align="right"
                                 sx={{ fontVariantNumeric: 'tabular-nums' }}>
                        {f.duration.toFixed(1)}s
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          </Paper>

          {r.slowest.length > 0 && (
            <Paper variant="outlined" sx={{ p: 2 }} data-testid="slowest">
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
                Slowest tests
              </Typography>
              <Stack spacing={0.5}>
                {r.slowest.map((s) => (
                  <Stack key={s.name} direction="row" spacing={1}
                         justifyContent="space-between">
                    <Typography variant="caption" sx={{ fontSize: 11 }}>
                      {s.name}
                    </Typography>
                    <Typography variant="caption"
                                sx={{ fontVariantNumeric: 'tabular-nums',
                                      fontWeight: 600 }}>
                      {s.duration.toFixed(2)}s
                    </Typography>
                  </Stack>
                ))}
              </Stack>
            </Paper>
          )}
        </>
      ) : null}

      <ReasonCodeDialog
        open={ask}
        busy={busy}
        error={runError}
        title="Run the test suite"
        description={
          <>
            Runs the full suite on this host. It takes about three minutes and
            competes for CPU with any migration currently running, so prefer a
            quiet moment on a busy box.
          </>
        }
        onCancel={() => { setAsk(false); setRunError(null) }}
        onConfirm={async (reason: string) => {
          setBusy(true); setRunError(null)
          try {
            const res = await runTests(reason)
            if (!res.ok) throw new Error(res.detail || 'could not start')
            setAsk(false)
            setStarted(res.detail || 'test run started')
            refresh()
          } catch (e: any) {
            setRunError(e.message)
          } finally {
            setBusy(false)
          }
        }}
      />
    </Box>
  )
}

export default TestReport
