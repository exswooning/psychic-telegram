/**
 * The three chart shapes the job views need, so each chart is a line of
 * configuration instead of forty lines of axes.
 *
 * A chart with no data says so in words (`empty`) rather than drawing bare
 * axes: an empty plot reads as "zero", and "nothing recorded yet" is a
 * different fact.
 */
import React from 'react'
import { Box, Paper, Typography } from '@mui/material'
import {
  Area, Bar, BarChart, CartesianGrid, ComposedChart, Legend, Line,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { useChartStyle } from '@/hooks/useChartStyle'

export const ChartFrame: React.FC<{
  title: string; hint?: string; height?: number
  /** Why there is nothing to draw. Set = the chart is replaced by this. */
  empty?: string | null
  children: React.ReactElement
}> = ({ title, hint, height = 170, empty, children }) => (
  <Paper variant="outlined" sx={{ p: 1.5 }} data-testid={`chart-${title}`}>
    <Typography variant="caption" sx={{ fontWeight: 700, display: 'block' }}>{title}</Typography>
    {hint && (
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
        {hint}
      </Typography>
    )}
    {empty
      ? <Box sx={{ height: 60, display: 'flex', alignItems: 'center' }}>
          <Typography variant="caption" color="text.disabled">{empty}</Typography>
        </Box>
      : <ResponsiveContainer width="100%" height={height}>{children}</ResponsiveContainer>}
  </Paper>
)

export interface Series {
  key: string; name: string; color: string
  /** How to draw it. "step" holds each value until the next change, joined
   *  across gaps; "dots" marks points only (events on top of a line). */
  type?: 'area' | 'line' | 'bar' | 'step' | 'dots'
  /** Series-only: put it on the right-hand axis (a second unit). */
  right?: boolean
  stackId?: string
}
type Row = Record<string, string | number>

/** Values against a category or time axis: area, line and bars, mixable, with
 *  an optional second unit on the right. */
export const SeriesChart: React.FC<{
  data: Row[]; xKey: string; series: Series[]
  fmt?: (v: number) => string; fmtRight?: (v: number) => string
  /** Put the x values on a true time scale (points spaced by when they
   *  happened, not by how many there are), formatted by this. */
  timeFmt?: (epochSec: number) => string
  noLegend?: boolean
  /** Injected by ResponsiveContainer, which sizes its DIRECT child -- and
   *  this wrapper is that child, so it has to hand them on to the chart. */
  width?: number; height?: number
}> = ({ data, xKey, series, fmt, fmtRight, timeFmt, noLegend, width, height }) => {
  const s = useChartStyle()
  const hasRight = series.some((x) => x.right)
  return (
    <ComposedChart width={width} height={height} data={data}
                   margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
      <CartesianGrid strokeDasharray="3 3" stroke={s.grid} vertical={false} />
      {timeFmt
        ? <XAxis type="number" scale="time" dataKey={xKey} domain={['dataMin', 'dataMax']}
                 tickFormatter={timeFmt} stroke={s.axis} fontSize={10} tickLine={false}
                 axisLine={false} minTickGap={40} />
        : <XAxis dataKey={xKey} stroke={s.axis} fontSize={10} tickLine={false} axisLine={false}
                 minTickGap={24} />}
      <YAxis yAxisId="l" stroke={s.axis} fontSize={10} tickLine={false} axisLine={false}
             width={44} tickFormatter={fmt} allowDecimals />
      {hasRight && (
        <YAxis yAxisId="r" orientation="right" stroke={s.axis} fontSize={10} tickLine={false}
               axisLine={false} width={62} tickFormatter={fmtRight} />
      )}
      <Tooltip contentStyle={s.tooltip}
               labelFormatter={timeFmt ? (v) => timeFmt(Number(v)) : undefined} />
      {series.length > 1 && !noLegend && <Legend wrapperStyle={{ fontSize: 11 }} />}
      {series.map((x) => {
        const yAxisId = x.right ? 'r' : 'l'
        if (x.type === 'step') {
          // Held until the next change, and joined across the rows that
          // belong to other series -- the shape of a rate that only moves
          // when the controller moves it.
          return <Line key={x.key} yAxisId={yAxisId} type="stepAfter" connectNulls dataKey={x.key}
                       name={x.name} stroke={x.color} strokeWidth={2} dot={false}
                       isAnimationActive={false} />
        }
        if (x.type === 'dots') {
          return <Line key={x.key} yAxisId={yAxisId} dataKey={x.key} name={x.name} stroke="none"
                       dot={{ r: 3.5, fill: x.color, stroke: x.color }} activeDot={false}
                       isAnimationActive={false} />
        }
        if (x.type === 'line') {
          return <Line key={x.key} yAxisId={yAxisId} type="monotone" dataKey={x.key} name={x.name}
                       stroke={x.color} strokeWidth={2} dot={false} isAnimationActive={false} />
        }
        if (x.type === 'area') {
          return <Area key={x.key} yAxisId={yAxisId} type="monotone" dataKey={x.key} name={x.name}
                       stroke={x.color} fill={x.color} fillOpacity={0.18} strokeWidth={1.5}
                       isAnimationActive={false} />
        }
        return <Bar key={x.key} yAxisId={yAxisId} dataKey={x.key} name={x.name} fill={x.color}
                    stackId={x.stackId} isAnimationActive={false} />
      })}
    </ComposedChart>
  )
}

/** Bars per category. `horizontal` puts the categories down the side, which
 *  is the only layout that fits long names (operations, users, warnings). */
export const BarsChart: React.FC<{
  data: Row[]; xKey: string; series: Series[]
  horizontal?: boolean; fmt?: (v: number) => string; labelWidth?: number
  width?: number; height?: number   // injected by ResponsiveContainer
}> = ({ data, xKey, series, horizontal, fmt, labelWidth = 110, width, height }) => {
  const s = useChartStyle()
  return (
    <BarChart width={width} height={height} data={data}
              layout={horizontal ? 'vertical' : 'horizontal'}
              margin={{ top: 4, right: 12, bottom: 0, left: 0 }}>
      <CartesianGrid strokeDasharray="3 3" stroke={s.grid} vertical={!!horizontal}
                     horizontal={!horizontal} />
      {horizontal ? (
        <>
          <XAxis type="number" stroke={s.axis} fontSize={10} tickLine={false} axisLine={false}
                 tickFormatter={fmt} />
          <YAxis type="category" dataKey={xKey} width={labelWidth} fontSize={10.5}
                 stroke={s.axis} tickLine={false} axisLine={false} interval={0} />
        </>
      ) : (
        <>
          <XAxis dataKey={xKey} stroke={s.axis} fontSize={10} tickLine={false} axisLine={false}
                 minTickGap={12} />
          <YAxis stroke={s.axis} fontSize={10} tickLine={false} axisLine={false} width={44}
                 tickFormatter={fmt} />
        </>
      )}
      <Tooltip contentStyle={s.tooltip} cursor={{ fill: 'rgba(127,127,127,0.12)' }} />
      {series.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
      {series.map((x) => (
        <Bar key={x.key} dataKey={x.key} name={x.name} fill={x.color} stackId={x.stackId}
             radius={x.stackId ? 0 : [0, 3, 3, 0]} isAnimationActive={false} />
      ))}
    </BarChart>
  )
}
