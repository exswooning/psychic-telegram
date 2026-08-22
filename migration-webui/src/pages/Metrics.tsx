import React, { useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  Alert, Box, Chip, CircularProgress, IconButton, Paper, Stack,
  Table, TableBody, TableCell, TableHead, TableRow, Tooltip, Typography,
} from '@mui/material'
import { Refresh as RefreshIcon } from '@mui/icons-material'
import {
  fetchMetrics, fetchMyMetrics, MetricsSnapshot, LimiterState,
} from '@/api/controlPlane'

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

const bytes = (n: number) => {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(0)} MB`
  if (n >= 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${n} B`
}

/** Statuses that are not failures, so the volume table can colour honestly.
 *  SKIPPED_* covers a family (unexportable, too large, no permission) that
 *  are decisions rather than errors, and colouring them red is how a clean
 *  run teaches people to ignore red. */
const isFailure = (status: string) =>
  status === 'FAILED' || status === 'BLOCKED'

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
  const scoped = Number.isFinite(id) && id > 0
  const [m, setM] = useState<MetricsSnapshot | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(() => {
    setLoading(true)
    // Reached both from a migration (scoped) and from the sidebar, where
    // there is no account in context and the server picks the running one.
    ;(scoped ? fetchMetrics(id) : fetchMyMetrics())
      .then((r) => { setM(r); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [id, scoped])

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

          <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="operations">
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
          {m.volume && m.volume.length > 0 && (
            <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="volume">
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
                Items by type and outcome
              </Typography>
              {/* Counts, never averaged into a percentage. DONE, FAILED,
                  SKIPPED and BLOCKED coexist in one run and mean different
                  things; a single "94% complete" hides all four. */}
              <Box sx={{ overflowX: 'auto' }}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>type</TableCell>
                      <TableCell>outcome</TableCell>
                      <TableCell align="right">count</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {m.volume.map((v) => (
                      <TableRow key={`${v.itemType}-${v.status}`}
                                data-testid={`vol-${v.itemType}-${v.status}`}>
                        <TableCell sx={{ fontSize: 12 }}>{v.itemType}</TableCell>
                        <TableCell sx={{ fontSize: 12 }}>
                          <Chip size="small" variant="outlined"
                                color={isFailure(v.status) ? 'error'
                                  : v.status === 'SUCCESS' ? 'success' : 'default'}
                                label={v.status.toLowerCase()} />
                        </TableCell>
                        <TableCell align="right"
                                   sx={{ fontVariantNumeric: 'tabular-nums',
                                         fontWeight: 600,
                                         color: isFailure(v.status)
                                           ? 'error.main' : undefined }}>
                          {v.count.toLocaleString()}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            </Paper>
          )}

          {m.transfer && (
            <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="transfer">
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1.5 }}>
                Transfer today
              </Typography>
              {/* Google's 750 GB/day cap is per target account and is the
                  reason a run can stop mid-way with nothing having failed. */}
              <Stack direction="row" spacing={4} sx={{ flexWrap: 'wrap', gap: 2 }}>
                <Stat id="bytes" label="uploaded today"
                      value={bytes(m.transfer.bytesToday)} />
                <Stat id="cap" label="daily cap"
                      value={bytes(m.transfer.dailyCapBytes)}
                      hint="Google's per-account limit. A run stops when it is reached, which is a quota event and not a failure." />
                <Stat id="capleft" label="remaining"
                      value={bytes(Math.max(0, m.transfer.dailyCapBytes - m.transfer.bytesToday))}
                      tone={m.transfer.bytesToday / m.transfer.dailyCapBytes > 0.9
                        ? 'warn' : undefined} />
              </Stack>
            </Paper>
          )}

          {m.host && (
            <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="host">
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1.5 }}>
                Host capacity
              </Typography>
              <Stack direction="row" spacing={4} sx={{ flexWrap: 'wrap', gap: 2 }}>
                <Stat id="cores" label="cores" value={String(m.host.cores)}
                      hint="Not part of worker sizing: the pools are I/O-bound and a core multiplier bound before RAM ever did." />
                <Stat id="ram" label="RAM usable"
                      value={`${m.host.ramUsableGb} / ${m.host.ramTotalGb} GB`} />
                <Stat id="uworkers" label="migrate workers"
                      value={String(m.host.userWorkers)} />
                <Stat id="sworkers" label="seed workers"
                      value={String(m.host.seedWorkers)} />
                <Stat id="mbworker" label="MB budgeted/worker"
                      value={String(m.host.mbPerWorker)}
                      hint="Derived from the download chunk size, not a constant." />
                {m.host.underMemoryPressure && (
                  <Stat id="pressure" label="memory pressure"
                        value="yes" tone="error" />
                )}
              </Stack>
              <Typography variant="caption" color="text.secondary"
                          sx={{ mt: 1.5, display: 'block' }}
                          data-testid="host-reason">
                {m.host.reason}
              </Typography>
            </Paper>
          )}

          {m.mappings && m.mappings.length > 0 && (
            <Paper variant="outlined" sx={{ p: 2 }} data-testid="mappings">
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
                Live mappings on the target
              </Typography>
              {/* Distinct from the volume table above: audit_log records
                  every attempt ever made, id_mapping records what currently
                  exists. They disagree exactly when the target has lost
                  items the ledger still claims -- see verify-ledger. */}
              <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 0.5 }}>
                {m.mappings.map((x) => (
                  <Chip key={x.type} size="small" variant="outlined"
                        data-testid={`map-${x.type}`}
                        label={`${x.type} · ${x.count.toLocaleString()}`} />
                ))}
              </Stack>
            </Paper>
          )}
        </>
      )}
    </Box>
  )
}

export default Metrics
