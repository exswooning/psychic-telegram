import React from 'react'
import { Box, Typography } from '@mui/material'
import { useNavigate } from 'react-router-dom'
import { fmtDuration, useAb } from '@/hooks/useAb'

/** In the header on every page while an A/B arm is running: where it is and how long is left. */
const BenchmarkPill: React.FC = () => {
  const view = useAb()
  const navigate = useNavigate()
  const p = view?.progress
  if (!view?.running || !p) return null
  const cur = p.current
  return (
    <Box
      role="button" tabIndex={0} data-testid="benchmark-pill"
      aria-label="Benchmark running — open Benchmarks"
      onClick={() => navigate('/benchmarks')}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); navigate('/benchmarks') } }}
      sx={{ display: { xs: 'none', md: 'flex' }, alignItems: 'center', gap: 1, ml: 1,
            border: '1px solid', borderColor: 'secondary.main', borderRadius: 999, px: 2, py: 0.75,
            cursor: 'pointer' }}>
      <Typography variant="caption" sx={{ fontWeight: 700 }} noWrap>
        Benchmark {Math.min((p.done ?? 0) + 1, p.total ?? 0)}/{p.total}
        {cur ? ` · ${cur.mode}${cur.mailMode ? '+' + cur.mailMode : ''} · ${cur.stage}` : ''}
      </Typography>
      <Typography variant="caption" color="text.secondary" noWrap>
        {p.etaSeconds != null ? `~${fmtDuration(p.etaSeconds)} left` : 'estimating'}
      </Typography>
    </Box>
  )
}

export default BenchmarkPill
