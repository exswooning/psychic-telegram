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
import { Box, Typography } from '@mui/material'
import type { MetricsSnapshot } from '@/api/controlPlane'
import { useChartStyle } from '@/hooks/useChartStyle'
import { BarsChart, ChartFrame, SeriesChart } from '@/components/Charts'
import {
  dayRows, historyRows, limiterRows, operationRows, progressRow, transferRow, volumeRows,
} from '@/utils/metricsSeries'

const n = (v: number) => v.toLocaleString()
const ms = (v: number) => `${v}ms`
const gb = (v: number) => `${v} GB`

export const MigrateMetricsCharts: React.FC<{ m: MetricsSnapshot }> = ({ m }) => {
  const { c } = useChartStyle()
  const hist = historyRows(m.history)
  const ops = operationRows(m.operations)
  const lim = limiterRows(m.limiters)
  const vol = volumeRows(m.volume)
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
      <Typography variant="overline" color="text.secondary">Charts</Typography>
      <Box sx={{ display: 'grid', gap: 1.5, mt: 0.5,
                 gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', alignItems: 'start' }}>
        <ChartFrame title="Requests per second" hint={`last ${hist.length} snapshots`} empty={needTwo}>
          <SeriesChart data={hist} xKey="t"
                       series={[{ key: 'rps', name: 'requests/s', color: c.primary, type: 'area' }]} />
        </ChartFrame>
        <ChartFrame title="p95 latency" hint="Google queues before it rejects — a climb is the early warning"
                    empty={needTwo}>
          <SeriesChart data={hist} xKey="t" fmt={ms}
                       series={[{ key: 'p95Ms', name: 'p95', color: c.warning, type: 'line' }]} />
        </ChartFrame>
        <ChartFrame title="Failures per snapshot" empty={needTwo}>
          <SeriesChart data={hist} xKey="t"
                       series={[{ key: 'failures', name: 'failures', color: c.error }]} />
        </ChartFrame>

        <ChartFrame title="Latency by operation" hint="slowest first" height={Math.max(150, ops.length * 30)}
                    empty={none(ops, 'No calls recorded yet.')}>
          <BarsChart data={ops} xKey="label" horizontal fmt={ms} labelWidth={165}
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
