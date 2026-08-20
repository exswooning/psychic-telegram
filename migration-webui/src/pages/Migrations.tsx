import React, { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert, Box, Button, Chip, CircularProgress, IconButton, LinearProgress,
  Paper, Stack, Typography,
} from '@mui/material'
import {
  Refresh as RefreshIcon, ArrowForward as ArrowIcon,
  AddCircleOutline as NewIcon, ExpandMore as ExpandIcon,
} from '@mui/icons-material'
import { fetchMigrations, MigrationRow } from '@/api/controlPlane'

/**
 * Migrations — every tenant pair, and what each one is doing.
 *
 * Counts, never a single percentage. DONE / RUNNING / FAILED / PENDING
 * coexist in every real batch, and averaging them is the one thing tui.py's
 * design notes say never to do: a run that is 60% done and 40% failed is not
 * 60% of a migration. The bar below shows only what finished, with the other
 * states named beside it rather than folded in.
 */

const Counts: React.FC<{ row: MigrationRow }> = ({ row }) => {
  const p = row.progress
  return (
    <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 0.5 }}>
      <Chip size="small" variant="outlined" color="success"
            label={`${p.done.toLocaleString()} done`} />
      {p.running > 0 && (
        <Chip size="small" variant="outlined" color="primary"
              label={`${p.running.toLocaleString()} running`} />
      )}
      {p.pending > 0 && (
        <Chip size="small" variant="outlined"
              label={`${p.pending.toLocaleString()} pending`} />
      )}
      {p.failed > 0 && (
        <Chip size="small" color="error"
              label={`${p.failed.toLocaleString()} failed`} />
      )}
    </Stack>
  )
}

export const Migrations: React.FC = () => {
  const navigate = useNavigate()
  const [rows, setRows] = useState<MigrationRow[]>([])
  const [maxConcurrent, setMaxConcurrent] = useState(1)
  const [activeTotal, setActiveTotal] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(() => {
    setLoading(true)
    fetchMigrations()
      .then((r) => {
        setRows(r.migrations)
        setMaxConcurrent(r.maxConcurrent)
        setActiveTotal(r.activeTotal)
        setError('')
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    refresh()
    // Fast enough to feel live on a running migration, slow enough that a
    // page left open overnight is not a load generator.
    const t = window.setInterval(refresh, 5000)
    return () => window.clearInterval(t)
  }, [refresh])

  const atCapacity = activeTotal >= maxConcurrent

  return (
    <Box sx={{ p: 3 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <Typography variant="h5" sx={{ fontWeight: 700 }}>Migrations</Typography>
        {loading && <CircularProgress size={16} />}
        <Box sx={{ flex: 1 }} />
        <Button size="small" variant="contained" startIcon={<NewIcon />}
                data-testid="new-migration"
                onClick={() => navigate('/wizard')}>
          Start a new migration
        </Button>
        <IconButton size="small" onClick={refresh} aria-label="refresh">
          <RefreshIcon fontSize="small" />
        </IconButton>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        One row per tenant pair. Up to {maxConcurrent} can run at once —
        each running migration gets a share of the machine&apos;s memory, so
        two at a time is two half-size pools, not two full ones.
      </Typography>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      {atCapacity && (
        <Alert severity="info" sx={{ mb: 2 }} data-testid="at-capacity">
          {activeTotal} of {maxConcurrent} slots in use. A new migration will
          be refused until one finishes — the cap exists because every worker
          costs real memory, and oversubscribing it stalls both runs rather
          than slowing them.
        </Alert>
      )}

      {rows.length === 0 && !loading && (
        <Paper variant="outlined" sx={{ p: 3 }}>
          <Typography variant="body2" color="text.secondary"
                      data-testid="no-migrations">
            No tenant pairs set up yet. Run the Setup Wizard to point at a
            source and a target — an account with neither configured is not a
            migration, so nothing is listed for it.
          </Typography>
        </Paper>
      )}

      <Stack spacing={2}>
        {rows.map((row) => {
          const p = row.progress
          const pct = p.users ? Math.round(100 * p.done / p.users) : 0
          return (
            <Paper key={row.accountId} variant="outlined"
                   data-testid={`migration-${row.accountId}`}
                   sx={{ p: 2, cursor: 'pointer',
                         '&:hover': { borderColor: 'primary.main' } }}
                   onClick={() => navigate('/running-now')}>
              <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
                <Typography sx={{ fontWeight: 700, fontSize: 15 }}>
                  {row.sourceDomain || '—'}
                </Typography>
                <ArrowIcon fontSize="small" color="disabled" />
                <Typography sx={{ fontWeight: 700, fontSize: 15 }}>
                  {row.targetDomain || '—'}
                </Typography>
                {row.running ? (
                  <Chip size="small" color="primary"
                        label={row.jobs.join(', ') || 'running'} />
                ) : (
                  <Chip size="small" variant="outlined" label="idle" />
                )}
                <Box sx={{ flex: 1 }} />
                <Typography variant="caption" color="text.secondary">
                  {row.accountName}
                </Typography>
                <ExpandIcon fontSize="small" color="disabled"
                            sx={{ transform: 'rotate(-90deg)' }} />
              </Stack>

              {/* Only what FINISHED drives the bar. Folding failures into it
                  would make a half-failed run look half-done, which is the
                  one reading that stops anyone investigating. */}
              <LinearProgress variant="determinate" value={pct}
                              sx={{ mb: 1, borderRadius: 1, height: 6 }} />

              <Stack direction="row" alignItems="center" spacing={2}
                     sx={{ flexWrap: 'wrap', gap: 1 }}>
                <Typography variant="caption" color="text.secondary"
                            data-testid={`users-${row.accountId}`}>
                  {p.done.toLocaleString()} of {p.users.toLocaleString()} users
                </Typography>
                <Counts row={row} />
                <Box sx={{ flex: 1 }} />
                <Typography variant="caption" color="text.secondary">
                  {p.items.toLocaleString()} items migrated
                  {p.itemsFailed > 0 && `, ${p.itemsFailed.toLocaleString()} failed`}
                </Typography>
              </Stack>
            </Paper>
          )
        })}
      </Stack>
    </Box>
  )
}

export default Migrations
