import React, { useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  Alert, Box, Chip, CircularProgress, IconButton, Paper, Stack,
  Table, TableBody, TableCell, TableHead, TableRow, Tooltip, Typography,
} from '@mui/material'
import { Refresh as RefreshIcon } from '@mui/icons-material'
import { fetchMetrics, MetricsSnapshot, LimiterState } from '@/api/controlPlane'

/**
 * What the migration is actually doing, per operation.
 *
 * Every number here is recorded by the MIGRATING process and read back from
 * the ledger. The previous dashboard called METRICS.snapshot() inside the API
 * server -- a process that issues no Drive calls -- and rendered that empty
 * reservoir as the run's performance, so the page was always honest-looking
 * and always zero.
 *
 * Operations are sorted slowest-first because "which call is costing the run"
 * is the only question this page exists to answer; fourteen labels in
 * alphabetical order do not answer it.
 */

const ms = (seconds: number) =>
  seconds >= 1 ? `${seconds.toFixed(2)}s` : `${Math.round(seconds * 1000)}ms`

const Stat: React.FC<{
  id: string; label: string; value: string; hint?: string; tone?: 'error' | 'warn'
}> = ({ id, label, value, hint, tone }) => (
  <Box data-testid={`metric-${id}`} sx={{ minWidth: 110 }}>
    <Typography sx={{
      fontWeight: 700, fontSize: 22, lineHeight: 1.2,
      fontVariantNumeric: 'tabular-nums',
      color: tone === 'error' ? 'error.main'
        : tone === 'warn' ? 'warning.main' : 'text.primary',
    }}>
      {value}
    </Typography>
    <Tooltip title={hint || ''} placement="bottom-start">
      <Typography variant="caption" color="text.secondary"
                  sx={{ borderBottom: hint ? '1px dotted' : 'none' }}>
        {label}
      </Typography>
    </Tooltip>
  </Box>
)

/** A rate limiter that found its own ceiling, and what it cost to find it. */
const Limiter: React.FC<{ tenant: string; s: LimiterState }> = ({ tenant, s }) => {
  const atCeiling = s.rate >= s.ceiling
  const atFloor = s.rate <= s.floor
  return (
    <Box data-testid={`limiter-${tenant}`} sx={{ minWidth: 220 }}>
      <Stack direction="row" spacing={1} alignItems="baseline">
        <Typography sx={{ fontWeight: 700, fontSize: 20,
                          fontVariantNumeric: 'tabular-nums' }}>
          {s.rate.toFixed(1)}/s
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {tenant} project
        </Typography>
      </Stack>
      <Stack direction="row" spacing={0.5} sx={{ mt: 0.5, flexWrap: 'wrap', gap: 0.5 }}>
        {/* A limiter pinned at its ceiling is no longer the thing deciding
            the rate, which is worth saying rather than leaving to be
            inferred from two numbers being equal. */}
        {atCeiling && (
          <Chip size="small" color="info" variant="outlined"
                label="at ceiling — not the binding limit" />
        )}
        {atFloor && (
          <Chip size="small" color="warning"
                label="at floor — heavily throttled" />
        )}
        <Chip size="small" variant="outlined"
              label={`${s.backoffs} backoff${s.backoffs === 1 ? '' : 's'}`} />
        {s.rejections > 0 && (
          <Chip size="small" color="warning" variant="outlined"
                label={`${s.rejections.toLocaleString()} quota rejection${
                  s.rejections === 1 ? '' : 's'}`} />
        )}
      </Stack>
    </Box>
  )
}

export const Metrics: React.FC = () => {
  const { accountId } = useParams()
  const id = Number(accountId)
  const [m, setM] = useState<MetricsSnapshot | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(() => {
    if (!Number.isFinite(id)) return
    setLoading(true)
    fetchMetrics(id)
      .then((r) => { setM(r); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [id])

  useEffect(() => {
    refresh()
    const t = window.setInterval(refresh, 10_000)
    return () => window.clearInterval(t)
  }, [refresh])

  const l = m?.latest

  return (
    <Box sx={{ p: 3 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 2 }}>
        <Typography variant="h5" sx={{ fontWeight: 700 }}>Performance</Typography>
        {loading && <CircularProgress size={16} />}
        <Box sx={{ flex: 1 }} />
        <IconButton size="small" onClick={refresh} aria-label="refresh">
          <RefreshIcon fontSize="small" />
        </IconButton>
      </Stack>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {m?.error && (
        <Alert severity="info" sx={{ mb: 2 }} data-testid="metrics-empty">
          {m.error}
        </Alert>
      )}

      {l && (
        <>
          <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
            <Stack direction="row" spacing={4} sx={{ flexWrap: 'wrap', gap: 2 }}>
              <Stat id="rps" label="requests/sec"
                    value={l.requestsPerSec.toFixed(1)} />
              <Stat id="rpsw" label="per worker"
                    value={l.requestsPerSecPerWorker.toFixed(2)}
                    hint="Throughput divided by the number of worker threads that actually issued calls." />
              <Stat id="workers" label="workers" value={String(l.workers)} />
              <Stat id="calls" label="API calls" value={l.calls.toLocaleString()} />
              <Stat id="p50" label="p50 latency" value={ms(l.p50)} />
              <Stat id="p95" label="p95 latency" value={ms(l.p95)} />
              <Stat id="p99" label="p99 latency" value={ms(l.p99)} />
              <Stat id="retries" label="retries"
                    value={l.retries.toLocaleString()}
                    tone={l.retries > 0 ? 'warn' : undefined}
                    hint="A retried call succeeded eventually. Counted separately from failures because the two need different responses." />
              <Stat id="failures" label="failures"
                    value={l.failures.toLocaleString()}
                    tone={l.failures > 0 ? 'error' : undefined} />
            </Stack>
            <Typography variant="caption" color="text.secondary"
                        sx={{ mt: 1.5, display: 'block' }}>
              recorded {l.recordedAt} · run elapsed {Math.round(l.elapsedSec)}s
            </Typography>
          </Paper>

          {Object.keys(m.limiters).length > 0 && (
            <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="limiters">
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1.5 }}>
                Rate limiters
              </Typography>
              {/* Source and target are different GCP projects, metered
                  separately by Google, so they are shown separately here. */}
              <Stack direction="row" spacing={4} sx={{ flexWrap: 'wrap', gap: 3 }}>
                {Object.entries(m.limiters).map(([tenant, s]) => (
                  <Limiter key={tenant} tenant={tenant} s={s} />
                ))}
              </Stack>
            </Paper>
          )}

          <Paper variant="outlined" sx={{ p: 2 }} data-testid="operations">
            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
              By operation, slowest first
            </Typography>
            {m.operations.length === 0 ? (
              <Typography variant="body2" color="text.secondary">
                No operations recorded in this sample.
              </Typography>
            ) : (
              <Box sx={{ overflowX: 'auto' }}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>operation</TableCell>
                      <TableCell align="right">calls</TableCell>
                      <TableCell align="right">p50</TableCell>
                      <TableCell align="right">p95</TableCell>
                      <TableCell align="right">retries</TableCell>
                      <TableCell align="right">failures</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {m.operations.map((o) => (
                      <TableRow key={o.label} data-testid={`op-${o.label}`}>
                        <TableCell sx={{ fontSize: 12 }}>{o.label}</TableCell>
                        <TableCell align="right"
                                   sx={{ fontVariantNumeric: 'tabular-nums' }}>
                          {o.calls.toLocaleString()}
                        </TableCell>
                        <TableCell align="right"
                                   sx={{ fontVariantNumeric: 'tabular-nums' }}>
                          {ms(o.p50)}
                        </TableCell>
                        <TableCell align="right"
                                   sx={{ fontVariantNumeric: 'tabular-nums',
                                         fontWeight: 600 }}>
                          {ms(o.p95)}
                        </TableCell>
                        <TableCell align="right"
                                   sx={{ fontVariantNumeric: 'tabular-nums',
                                         color: o.retries ? 'warning.main' : undefined }}>
                          {o.retries.toLocaleString()}
                        </TableCell>
                        <TableCell align="right"
                                   sx={{ fontVariantNumeric: 'tabular-nums',
                                         color: o.failures ? 'error.main' : undefined }}>
                          {o.failures.toLocaleString()}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            )}
          </Paper>
        </>
      )}
    </Box>
  )
}

export default Metrics
