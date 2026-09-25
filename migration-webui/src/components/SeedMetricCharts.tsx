/**
 * The rest of what a seed run measures. SeedRunDashboard already draws users
 * finished and the totals by type; this adds the ones that answer "is it
 * healthy": how long users take, who is slowest, what is failing and where,
 * and -- for a fill run -- how much storage each account actually gained.
 */
import React from 'react'
import { Box, Typography } from '@mui/material'
import type { SeedRun } from '@/utils/seedLog'
import { useChartStyle } from '@/hooks/useChartStyle'
import { BarsChart, ChartFrame, SeriesChart } from '@/components/Charts'
import {
  durationBands, failedServiceRows, fillRateRows, fillRows, itemsAsUsersFinish, slowestUsers,
  throttleRows,
  storageRows, storageTotals, warningRows,
} from '@/utils/seedSeries'

const n = (v: number) => v.toLocaleString()

export const SeedMetricCharts: React.FC<{ run: SeedRun }> = ({ run }) => {
  const { c } = useChartStyle()
  const cum = itemsAsUsersFinish(run.users)
  const fill = fillRows(run.fillSamples)
  const rate = fillRateRows(run.fillSamples)
  const throttle = throttleRows(run.throttleSamples)
  const bands = durationBands(run.users)
  const slow = slowestUsers(run.users)
  const warn = warningRows(run)
  const failed = failedServiceRows(run.users)
  const store = storageRows(run.users)
  const total = storageTotals(run.users)
  const tall = (rows: unknown[]) => Math.max(120, rows.length * 28)

  return (
    <Box data-testid="seed-charts" sx={{ mb: 1.5 }}>
      <Box sx={{ display: 'grid', gap: 1.5,
                 gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', alignItems: 'start' }}>
        {fill.length > 0 && (
          <ChartFrame title="Uploaded to Drive over time"
                      hint="gigabytes actually written; planned counts the users started so far"
                      empty={fill.length < 2 ? 'Needs two heartbeats (one every 30 s).' : null}>
            <SeriesChart data={fill} xKey="t" fmt={(v) => `${v.toLocaleString()} GB`}
                         series={[{ key: 'uploaded', name: 'uploaded', color: c.success, type: 'area' },
                                  { key: 'planned', name: 'planned', color: c.muted, type: 'line' }]} />
          </ChartFrame>
        )}
        {rate.length > 0 && (
          <ChartFrame title="Upload rate" hint="GB per hour between heartbeats — dips are the box or Google slowing down"
                      empty={rate.length < 2 ? 'Needs three heartbeats.' : null}>
            <SeriesChart data={rate} xKey="t" fmt={(v) => `${v.toLocaleString()}`}
                         series={[{ key: 'gbPerHour', name: 'GB/h', color: c.primary, type: 'step' }]} />
          </ChartFrame>
        )}
        <ChartFrame title="Request rate and retries"
                    hint="calls/s, and the retries added in each 30 s — a retry is Google saying no. The seeder backs off and retries; it has no rate controller, so there is no limiter sawtooth here"
                    empty={throttle.length < 2 ? 'Needs two heartbeats that carry request figures (runs started after this update print them).' : null}>
          <SeriesChart data={throttle} xKey="t" fmt={(v) => v.toLocaleString()} fmtRight={(v) => v.toLocaleString()}
                       series={[{ key: 'reqPerSec', name: 'calls/s', color: c.primary, type: 'line' },
                                { key: 'retries', name: 'retries', color: c.warning, right: true }]} />
        </ChartFrame>
        {/* A fill writes filler files, not seeded items, so every finished
            user reads zero and the line is flat -- a chart of nothing. */}
        {!(cum.length >= 2 && cum[cum.length - 1].items === 0) && (
          <ChartFrame title="Items written as users finish" hint="cumulative, in finishing order"
                      empty={cum.length < 2 ? 'Needs two finished users.' : null}>
            <SeriesChart data={cum} xKey="users" fmt={n}
                         series={[{ key: 'items', name: 'items', color: c.success, type: 'area' }]} />
          </ChartFrame>
        )}
        <ChartFrame title="How long users take" hint="users per duration band"
                    empty={bands.length === 0 ? 'Needs two finished users.' : null}>
          <BarsChart data={bands} xKey="range"
                     series={[{ key: 'users', name: 'users', color: c.primary }]} />
        </ChartFrame>
        <ChartFrame title="Slowest users" hint="seconds" height={tall(slow)}
                    empty={slow.length === 0 ? 'No finished users yet.' : null}>
          <BarsChart data={slow} xKey="user" horizontal fmt={n} labelWidth={110}
                     series={[{ key: 'seconds', name: 'seconds', color: c.warning }]} />
        </ChartFrame>
        <ChartFrame title="Warnings" hint="by what the seeder complained about" height={tall(warn)}
                    empty={warn.length === 0 ? 'No warnings.' : null}>
          <BarsChart data={warn} xKey="name" horizontal fmt={n} labelWidth={150}
                     series={[{ key: 'count', name: 'count', color: c.error }]} />
        </ChartFrame>
        <ChartFrame title="Services that failed inside a user" height={tall(failed)}
                    empty={failed.length === 0 ? 'No service reported a failure.' : null}>
          <BarsChart data={failed} xKey="service" horizontal fmt={n} labelWidth={90}
                     series={[{ key: 'count', name: 'users', color: c.error }]} />
        </ChartFrame>
        {total && (
          <ChartFrame title="Storage filled per user"
                      hint={`${total.addedGb.toLocaleString()} GB added across ${total.users} user(s), ${total.fillers.toLocaleString()} filler file(s) — largest ${store.length} shown`}
                      height={tall(store)}>
            <BarsChart data={store} xKey="user" horizontal labelWidth={110}
                       fmt={(v) => `${v} GB`}
                       series={[{ key: 'before', name: 'was', color: c.muted, stackId: 's' },
                                { key: 'added', name: 'added', color: c.success, stackId: 's' }]} />
          </ChartFrame>
        )}
      </Box>
      {run.users.length > 0 && cum.length === 0 && (
        <Typography variant="caption" color="text.disabled">No user has finished yet.</Typography>
      )}
    </Box>
  )
}

export default SeedMetricCharts
