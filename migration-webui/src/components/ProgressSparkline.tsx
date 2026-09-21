import React from 'react'
import { useTheme } from '@mui/material'
import { AreaChart, Area, YAxis, ResponsiveContainer } from 'recharts'
import type { ProgressSample } from '@/hooks/useProgressHistory'

/**
 * The shape of a running job's own progress, not just its current number.
 *
 * A lone percentage says "62%"; this says whether that 62% arrived by
 * climbing steadily or by sitting at 40% for an hour and then jumping --
 * which is the difference between "on track" and "was stuck, then wasn't"
 * that no single figure can carry. Every point plotted is a real reported
 * pct (see useProgressHistory) -- nothing here is interpolated or guessed.
 *
 * Deliberately axis-less and small: this sits under a progress bar that
 * already states the current number precisely, so the chart's only job is
 * the trend, not a second place to read the same figure.
 */
export const ProgressSparkline: React.FC<{
  samples: ProgressSample[]
  height?: number
}> = ({ samples, height = 32 }) => {
  const theme = useTheme()
  // A single point has no trend to show -- rendering it would be a flat
  // line that claims more certainty than one sample supports.
  if (samples.length < 2) return null

  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={samples} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="sparklineFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={theme.palette.primary.main} stopOpacity={0.35} />
            <stop offset="100%" stopColor={theme.palette.primary.main} stopOpacity={0} />
          </linearGradient>
        </defs>
        <YAxis domain={[0, 100]} hide />
        <Area type="monotone" dataKey="pct" stroke={theme.palette.primary.main}
              strokeWidth={1.5} fill="url(#sparklineFill)" isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  )
}

export default ProgressSparkline
