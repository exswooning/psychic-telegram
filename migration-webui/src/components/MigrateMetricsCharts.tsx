/**
 * Every number a migration records, as a shape. The tiles and tables on the
 * Performance page keep the exact figures; this is what they cannot show --
 * whether a rate is climbing or stalling, which operation is the slow one,
 * how close a limiter is to its ceiling.
 *
 * Each chart owns its empty state: a series the run has not produced yet
 * says so instead of drawing an axis that reads as zero.
 */
import React from 'react'
import { Box, Paper, Stack, Typography } from '@mui/material'
import type { MetricsSnapshot } from '@/api/controlPlane'
import { useChartStyle } from '@/hooks/useChartStyle'
import { BarsChart, ChartFrame, PieChartFrame, SeriesChart } from '@/components/Charts'
import { Stat } from '@/pages/Metrics'
import {
  clockAt, dayRows, failureCauseRows, historyRows, historyStats, limiterRows, operationRows,
  progressRow, sawtoothRows, transferRow, volumeRows, volumeShareRows, msAxis,
} from '@/utils/metricsSeries'

const n = (v: number) => v.toLocaleString()
const ms = (v: number) => `${v}ms`
const gb = (v: number) => `${v} GB`
const pct = (v: number) => `${v}%`

export const MigrateMetricsCharts: React.FC<{ m: MetricsSnapshot }> = ({ m }) => {
  const { c } = useChartStyle()
  const hist = historyRows(m.history)
  const stats = historyStats(hist)
  const ops = operationRows(m.operations)
  const lim = limiterRows(m.limiters)
  // One entry per limiter, each shaped on its own.
  const saw = Object.keys(m.limiterHistory ?? {}).sort().map((name) => {
    const { rows, stats } = sawtoothRows({ [name]: m.limiterHistory![name] })
    return { name, rows, stat: stats[0] }
  }).filter((x) => x.stat)
  const sawColors = [c.primary, c.info, c.success, c.warning]
  const vol = volumeRows(m.volume)
  const volShare = volumeShareRows(m.volume)
  const causes = failureCauseRows(m.failures)
  // 6 distinct colors for up to 5 named causes + one "other" slice -- exactly enough
  // that no two slices in the same pie ever share a color.
  const causeColors = [c.error, c.warning, c.info, c.primary, c.success, c.muted]
  const days = dayRows(m.throughput)
  const prog = progressRow(m.throughput)
  const xfer = transferRow(m.transfer)
  const maps = m.mappings ?? []
  const h = m.host
  const needTwo = hist.length < 2 ? 'Needs two snapshots from the running migration.' : null
  const none = (rows: unknown[] | null | undefined, why: string) =>
    !rows || rows.length === 0 ? why : null

  return (
    <Box data-testid="migrate-charts">
      {stats && (
        <Paper variant="outlined" sx={{ p: 2, mb: 1.5 }} data-testid="stability-stats">
          <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
            Stability over the last {hist.length} snapshots
          </Typography>
          {/* Every chart below shows the SHAPE of these snapshots; these are the shape
              reduced to numbers, because "is the rate actually stable" is exactly the
              question a sawtooth chart forces someone to eyeball rather than answer.
              Coefficient of variation (stdev / mean) is unitless, so a source bucket
              pinned near 1,200/s and a target one at 60/s are comparable by the same
              number -- a raw stdev is not. */}
          <Stack direction="row" spacing={4} sx={{ flexWrap: 'wrap', gap: 2 }}>
            <Stat id="rps-mean" label="requests/s, mean ± stdev"
                  value={`${stats.rps.mean} ± ${stats.rps.stdev}`}
                  hint={`range ${stats.rps.min}–${stats.rps.max} across the window`} />
            <Stat id="rps-cv" label="rate variability (CV)"
                  value={stats.rps.cvPct === null ? '—' : `${stats.rps.cvPct}%`}
                  tone={stats.rps.cvPct !== null && stats.rps.cvPct > 40 ? 'warn' : undefined}
                  hint="Coefficient of variation: stdev as a % of the mean. Near 0 is a flat, held rate; above ~40% is a run still lurching between probes and pushbacks." />
            <Stat id="p95-mean" label="p95 latency, mean ± stdev"
                  value={`${ms(stats.p95Ms.mean)} ± ${ms(stats.p95Ms.stdev)}`} />
            <Stat id="window-retry-rate" label="retry rate"
                  value={pct(stats.retryRatePct)}
                  hint={`${stats.totalRetries.toLocaleString()} retries across ${stats.totalCalls.toLocaleString()} calls in this window`} />
            <Stat id="window-failure-rate" label="failure rate"
                  value={pct(stats.failureRatePct)}
                  tone={stats.failureRatePct > 0 ? 'error' : undefined}
                  hint={`${stats.totalFailures.toLocaleString()} failures across ${stats.totalCalls.toLocaleString()} calls in this window`} />
          </Stack>
        </Paper>
      )}
      <Typography variant="overline" color="text.secondary">Charts</Typography>
      <Box sx={{ display: 'grid', gap: 1.5, mt: 0.5,
                 gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', alignItems: 'start' }}>
        <ChartFrame title="Requests per second" hint={`last ${hist.length} snapshots, with a trailing 5-point average`}
                    empty={needTwo}>
          <SeriesChart data={hist} xKey="t"
                       series={[{ key: 'rps', name: 'requests/s', color: c.primary, type: 'area' },
                                { key: 'rpsAvg', name: '5-pt average', color: c.info, type: 'line' }]} />
        </ChartFrame>
        <ChartFrame title="Latency percentiles" hint="p50 / p95 / p99 — Google queues before it rejects, so a climb in the tail is the early warning"
                    empty={needTwo}>
          <SeriesChart data={hist} xKey="t" fmt={msAxis}
                       series={[{ key: 'p50Ms', name: 'p50', color: c.info, type: 'line' },
                                { key: 'p95Ms', name: 'p95', color: c.warning, type: 'line' },
                                { key: 'p99Ms', name: 'p99', color: c.error, type: 'line' }]} />
        </ChartFrame>
        <ChartFrame title="Tail latency spread (p99 − p50)"
                    hint="widening here means a growing share of calls are far slower than typical, even while p50 looks fine"
                    empty={needTwo}>
          <SeriesChart data={hist} xKey="t" fmt={msAxis}
                       series={[{ key: 'spreadMs', name: 'p99 − p50', color: c.warning, type: 'area' }]} />
        </ChartFrame>
        <ChartFrame title="Failures per snapshot" empty={needTwo}>
          <SeriesChart data={hist} xKey="t"
                       series={[{ key: 'failures', name: 'failures', color: c.error }]} />
        </ChartFrame>
        <ChartFrame title="Retry and failure rate" hint="% of that snapshot's own calls — normalizes for a burst of volume, which a raw count cannot"
                    empty={needTwo}>
          <SeriesChart data={hist} xKey="t" fmt={pct}
                       series={[{ key: 'retryRatePct', name: 'retry rate', color: c.warning, type: 'line' },
                                { key: 'failureRatePct', name: 'failure rate', color: c.error, type: 'line' }]} />
        </ChartFrame>

        <ChartFrame title="Latency by operation" hint="slowest first" height={Math.max(150, ops.length * 30)}
                    empty={none(ops, 'No calls recorded yet.')}>
          <BarsChart data={ops} xKey="label" horizontal fmt={msAxis} labelWidth={165}
                     series={[{ key: 'p50Ms', name: 'p50', color: c.info },
                              { key: 'p95Ms', name: 'p95', color: c.warning }]} />
        </ChartFrame>
        <ChartFrame title="Calls by operation" height={Math.max(150, ops.length * 30)}
                    empty={none(ops, 'No calls recorded yet.')}>
          <BarsChart data={ops} xKey="label" horizontal fmt={n} labelWidth={165}
                     series={[{ key: 'calls', name: 'calls', color: c.primary }]} />
        </ChartFrame>
        <ChartFrame title="Retries and failures by operation" height={Math.max(150, ops.length * 30)}
                    empty={none(ops.filter((o) => o.retries || o.failures), 'No retries or failures — a clean run.')}>
          <BarsChart data={ops} xKey="label" horizontal fmt={n} labelWidth={165}
                     series={[{ key: 'retries', name: 'retries', color: c.warning },
                              { key: 'failures', name: 'failures', color: c.error }]} />
        </ChartFrame>
        <ChartFrame title="Retry/failure rate by operation" hint="% of that operation's own calls — five retries out of ten and five out of ten thousand are not the same fact"
                    height={Math.max(150, ops.length * 30)}
                    empty={none(ops.filter((o) => o.retryPct || o.failurePct), 'No retries or failures — a clean run.')}>
          <BarsChart data={ops} xKey="label" horizontal fmt={pct} labelWidth={165}
                     series={[{ key: 'retryPct', name: 'retry rate', color: c.warning },
                              { key: 'failurePct', name: 'failure rate', color: c.error }]} />
        </ChartFrame>
        <ChartFrame title="Failure causes" height={280}
                    hint="which OPERATION failed is above; this is WHY -- grouped by the underlying error with file ids and links stripped, so one cause repeated across a thousand files is one slice, not a thousand"
                    empty={none(causes, 'No failures recorded — a clean run.')}>
          <PieChartFrame data={causes} nameKey="name" valueKey="value" colors={causeColors}
                         fmt={n} label={(row) => String(row.reason)} />
        </ChartFrame>

        {/* One chart PER limiter, full width. They differ by orders of
            magnitude (a source bucket pinned at 1,200/s beside a target one
            sawtoothing 45-85/s), so sharing an axis flattens exactly the
            shape this exists to show. */}
        {saw.length === 0 && (
          <Box sx={{ gridColumn: '1 / -1' }}>
            <ChartFrame title="Rate limiter sawtooth" empty="Needs limiter snapshots from the running migration.">
              <SeriesChart data={[]} xKey="ts" series={[]} />
            </ChartFrame>
          </Box>
        )}
        {saw.map(({ name, rows, stat }, i) => (
          <Box key={name} sx={{ gridColumn: '1 / -1' }}>
            <ChartFrame title={`Sawtooth · ${name}`} height={170}
                        hint={`${stat.pushbacks.toLocaleString()} pushback${stat.pushbacks === 1 ? '' : 's'}`
                          + `${stat.everySec != null ? `, one every ${Math.round(stat.everySec)}s` : ''}`
                          + ` · ${Math.round(stat.low)}–${Math.round(stat.high)} calls/s — climbs are the limiter probing for headroom, red dots are quota pushbacks from Google`}
                        empty={rows.length < 2 ? 'Needs two limiter snapshots.' : null}>
              <SeriesChart data={rows} xKey="ts" timeFmt={clockAt} noLegend
                           series={[
                             { key: name, name, color: sawColors[i % sawColors.length], type: 'step' },
                             { key: `${name} pushback`, name: 'pushback', color: c.error, type: 'dots' },
                           ]} />
            </ChartFrame>
          </Box>
        ))}
        <ChartFrame title="Rate limiters" hint="current rate between its floor and ceiling (calls/s)"
                    empty={none(lim, 'No limiter state recorded.')}>
          <BarsChart data={lim} xKey="name" horizontal labelWidth={90}
                     series={[{ key: 'floor', name: 'floor', color: c.muted },
                              { key: 'rate', name: 'rate', color: c.primary },
                              { key: 'ceiling', name: 'ceiling', color: c.success }]} />
        </ChartFrame>
        <ChartFrame title="Limiter pushback" hint="quota rejections and backoffs"
                    empty={none(lim.filter((l) => l.rejections || l.backoffs), 'No pushback from Google.')}>
          <BarsChart data={lim} xKey="name" horizontal fmt={n} labelWidth={90}
                     series={[{ key: 'rejections', name: 'rejections', color: c.error },
                              { key: 'backoffs', name: 'backoffs', color: c.warning }]} />
        </ChartFrame>

        <ChartFrame title="Work per day" hint="items moved, and gigabytes"
                    empty={none(days, 'Nothing recorded in the ledger yet.')}>
          <SeriesChart data={days} xKey="day" fmt={n} fmtRight={gb}
                       series={[{ key: 'items', name: 'items', color: c.primary },
                                { key: 'gb', name: 'GB', color: c.success, type: 'line', right: true }]} />
        </ChartFrame>
        <ChartFrame title="Outcome by item type" height={Math.max(150, vol.length * 30)}
                    empty={none(vol, 'Nothing recorded in the ledger yet.')}>
          <BarsChart data={vol} xKey="itemType" horizontal fmt={n} labelWidth={100}
                     series={[{ key: 'done', name: 'done', color: c.success, stackId: 'o' },
                              { key: 'skipped', name: 'skipped', color: c.muted, stackId: 'o' },
                              { key: 'failed', name: 'failed', color: c.error, stackId: 'o' }]} />
        </ChartFrame>
        <ChartFrame title="Outcome share by item type" hint="each type's own 100% — a rare type that failed entirely is invisible on the raw-count chart beside a huge one"
                    height={Math.max(150, volShare.length * 30)}
                    empty={none(volShare, 'Nothing recorded in the ledger yet.')}>
          <BarsChart data={volShare} xKey="itemType" horizontal fmt={pct} labelWidth={100}
                     series={[{ key: 'done', name: 'done', color: c.success, stackId: 'os' },
                              { key: 'skipped', name: 'skipped', color: c.muted, stackId: 'os' },
                              { key: 'failed', name: 'failed', color: c.error, stackId: 'os' }]} />
        </ChartFrame>
        <ChartFrame title="Items done and remaining" hint="counts, from the run's own expected total"
                    height={90} empty={prog ? null : 'No expected total recorded yet.'}>
          <BarsChart data={prog ?? []} xKey="name" horizontal fmt={n} labelWidth={40}
                     series={[{ key: 'done', name: 'done', color: c.success, stackId: 'p' },
                              { key: 'remaining', name: 'remaining', color: c.muted, stackId: 'p' }]} />
        </ChartFrame>
        <ChartFrame title="Uploaded today against the daily cap" hint="gigabytes" height={90}
                    empty={xfer ? null : 'No daily cap configured.'}>
          <BarsChart data={xfer ?? []} xKey="name" horizontal fmt={gb} labelWidth={40}
                     series={[{ key: 'used', name: 'uploaded', color: c.primary, stackId: 't' },
                              { key: 'left', name: 'left under cap', color: c.muted, stackId: 't' }]} />
        </ChartFrame>

        <ChartFrame title="Live mappings on the target" height={Math.max(150, maps.length * 28)}
                    empty={none(maps, 'No mappings yet.')}>
          <BarsChart data={maps.map((x) => ({ type: x.type, count: x.count }))} xKey="type" horizontal
                     fmt={n} labelWidth={100}
                     series={[{ key: 'count', name: 'mapped', color: c.info }]} />
        </ChartFrame>
        <ChartFrame title="Workers against cores" hint="what this host budgeted" height={130}
                    empty={h ? null : 'Host sizing not reported.'}>
          <BarsChart data={h ? [{ name: 'cores', value: h.cores },
                                { name: 'migrate workers', value: h.userWorkers },
                                { name: 'seed workers', value: h.seedWorkers }] : []}
                     xKey="name" horizontal labelWidth={110}
                     series={[{ key: 'value', name: 'count', color: c.primary }]} />
        </ChartFrame>
      </Box>
    </Box>
  )
}

export default MigrateMetricsCharts
