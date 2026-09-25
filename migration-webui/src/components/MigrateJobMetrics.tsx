/**
 * The migration's charts inside a job's detail view: fetched on open and
 * refreshed while the job is running, so opening a card shows how the run is
 * going and not just how far along it claims to be.
 */
import React, { useEffect, useState } from 'react'
import { Alert, CircularProgress, Typography } from '@mui/material'
import { fetchMyMetrics, MetricsSnapshot } from '@/api/controlPlane'
import MigrateMetricsCharts from '@/components/MigrateMetricsCharts'

export const MigrateJobMetrics: React.FC<{ live: boolean }> = ({ live }) => {
  const [m, setM] = useState<MetricsSnapshot | null>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    let on = true
    const load = () => fetchMyMetrics(120)
      .then((r) => { if (on) { setM(r); setErr('') } })
      .catch((e) => { if (on) setErr(e instanceof Error ? e.message : String(e)) })
    load()
    const t = live ? window.setInterval(load, 5_000) : undefined
    return () => { on = false; if (t) window.clearInterval(t) }
  }, [live])

  if (err) return <Alert severity="warning" sx={{ mb: 2 }}>Metrics unavailable: {err}</Alert>
  if (!m) return <CircularProgress size={18} />
  if (m.error) return <Typography variant="body2" color="text.secondary">{m.error}</Typography>
  return <MigrateMetricsCharts m={m} />
}

export default MigrateJobMetrics
