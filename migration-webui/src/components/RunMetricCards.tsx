import React, { useEffect, useState } from 'react'
import { Box, Card, CardContent, Chip, Grid, Stack, Typography } from '@mui/material'
import { fetchMetricRuns, RunMetrics } from '@/api/controlPlane'
import { describeElapsed } from '@/hooks/useRunningJobs'

const ms = (v?: number) => (typeof v === 'number' ? `${Math.round(v)} ms` : '—')
const num = (v?: number | null, d = 0) => (typeof v === 'number' ? v.toLocaleString(undefined, { maximumFractionDigits: d }) : '—')

/**
 * Each run's numbers, kept after it ends (run_metric_summary) -- the live charts
 * above are the last hour of samples, rolled over. One card per run, titled with
 * the domains it ran between.
 */
const RunMetricCards: React.FC<{ accountId: number }> = ({ accountId }) => {
  const [runs, setRuns] = useState<RunMetrics[] | null>(null)
  useEffect(() => {
    let live = true
    const read = () => Promise.resolve().then(() => fetchMetricRuns(accountId))
      .then((r) => { if (live) setRuns(r?.runs ?? []) }).catch(() => {})
    read()
    const id = setInterval(read, 15000)
    return () => { live = false; clearInterval(id) }
  }, [accountId])

  if (!runs?.length) return null
  return (
    <Box sx={{ mb: 3 }} data-testid="run-metric-cards">
      <Typography variant="subtitle1" sx={{ fontWeight: 700, mb: 1 }}>Runs</Typography>
      <Grid container spacing={1.5}>
        {runs.map((r) => {
          const title = r.targetDomain ? `${r.sourceDomain} → ${r.targetDomain}` : (r.sourceDomain ?? 'unknown domain')
          return (
            <Grid item xs={12} sm={6} lg={4} key={r.runKey}>
              <Card variant="outlined" data-testid={`run-card-${r.runKey}`}>
                <CardContent>
                  <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
                    <Chip size="small" label={r.kind} />
                    <Typography variant="caption" color="text.secondary">
                      {new Date(r.startedAt).toLocaleString()} · {describeElapsed(r.elapsed_sec ?? 0)}
                    </Typography>
                  </Stack>
                  <Typography variant="subtitle2" sx={{ fontWeight: 700, wordBreak: 'break-word', mb: 1 }}>
                    {title}
                  </Typography>
                  <Grid container spacing={1}>
                    {[
                      ['calls', num(r.calls)],
                      ['req/s · peak', `${num(r.requests_per_sec, 1)} · ${num(r.peak_requests_per_sec, 1)}`],
                      ['p50 · p95', `${ms(r.p50)} · ${ms(r.p95)}`],
                      ['retries', num(r.retries)],
                      ['failures', num(r.failures)],
                      ['peak memory', typeof r.peak_rss_mb === 'number' ? `${num(r.peak_rss_mb)} MB` : '—'],
                    ].map(([label, value]) => (
                      <Grid item xs={6} key={label}>
                        <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>{label}</Typography>
                        <Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }}>{value}</Typography>
                      </Grid>
                    ))}
                  </Grid>
                </CardContent>
              </Card>
            </Grid>
          )
        })}
      </Grid>
    </Box>
  )
}

export default RunMetricCards
