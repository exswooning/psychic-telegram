/**
 * The waiting room.
 *
 * The box runs a fixed number of heavy jobs at once, and it used to refuse
 * everything past that with a 503: "capacity is full, try again shortly".
 * With one operator that is fine. With several accounts sharing a
 * deployment it means whoever retries at the right second wins, and
 * everyone else is told to try again by a page that shows nothing running
 * -- because the job filling the box belongs to somebody else, and no view
 * existed that could say so.
 *
 * So this panel answers exactly the questions that refusal raised: what is
 * on the box right now, how many are ahead of me, and what happened to the
 * thing I asked for. Other accounts' rows appear as a job name and a
 * position and nothing else; "you are third" is not information unless the
 * two ahead of you are visible.
 */
import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Card, CardContent, Chip, IconButton, LinearProgress, Stack,
  Tooltip, Typography,
} from '@mui/material'
import {
  Bolt as RunningIcon, HourglassTop as WaitingIcon,
  Close as CancelIcon, History as HistoryIcon,
} from '@mui/icons-material'
import { fetchQueue, cancelQueued } from '@/api/client'
import type { QueueEntry, QueueSnapshot } from '@/api/client'

const POLL_MS = 5000

/** "3 minutes ago", from an ISO stamp. Absolute times make an operator do
 *  timezone arithmetic to answer "has this been stuck?". */
export function ago(iso?: string): string {
  if (!iso) return ''
  const then = Date.parse(iso.endsWith('Z') ? iso : `${iso}Z`)
  if (Number.isNaN(then)) return ''
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`
  return `${Math.round(secs / 3600)}h ago`
}

const STATUS_COLOR: Record<string, 'success' | 'error' | 'default'> = {
  done: 'success', failed: 'error', cancelled: 'default',
}

/** Whose job this is, in the one form that is useful to both readers: the
 *  owner sees "yours", everyone else sees who to go and ask. */
function owner(row: QueueEntry): string {
  if (row.mine) return 'yours'
  return row.requestedBy || 'another account'
}

const Row: React.FC<{
  icon: React.ReactElement
  primary: string
  secondary: string
  action?: React.ReactNode
}> = ({ icon, primary, secondary, action }) => (
  <Stack direction="row" spacing={1.5} alignItems="center" sx={{ py: 0.75 }}>
    <Box sx={{ display: 'flex', color: 'text.secondary' }}>{icon}</Box>
    <Box sx={{ minWidth: 0, flex: 1 }}>
      <Typography variant="body2" sx={{ fontWeight: 600 }} noWrap>{primary}</Typography>
      <Typography variant="caption" color="text.secondary" noWrap
                  sx={{ display: 'block' }}>{secondary}</Typography>
    </Box>
    {action}
  </Stack>
)

export const QueuePanel: React.FC<{ compact?: boolean }> = ({ compact }) => {
  const [q, setQ] = useState<QueueSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(() => {
    fetchQueue().then((s) => { setQ(s); setError(null) })
      .catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, POLL_MS)
    return () => clearInterval(t)
  }, [refresh])

  const drop = async (id: number) => {
    const r = await cancelQueued(id)
    if (!r.ok) setError(r.error || 'could not cancel')
    refresh()
  }

  if (error) return <Alert severity="warning" variant="outlined">{error}</Alert>
  if (!q) return <LinearProgress />

  const free = Math.max(0, q.capacity - q.running.length)
  const history = compact ? [] : q.recent.filter((r) => r.status !== 'running')

  return (
    <Card variant="outlined">
      <CardContent>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 700, flex: 1 }}>
            Job queue
          </Typography>
          <Chip size="small"
                label={`${q.running.length} of ${q.capacity} slots busy`}
                color={free > 0 ? 'success' : 'warning'} variant="outlined" />
        </Stack>

        {q.running.length === 0 && q.waiting.length === 0 && (
          <Typography variant="body2" color="text.secondary">
            Nothing running and nothing waiting. A job you start now begins
            immediately.
          </Typography>
        )}

        {q.running.map((r, i) => (
          <Row key={`run-${i}`} icon={<RunningIcon fontSize="small" color="info" />}
               primary={r.jobName}
               secondary={`running ${ago(r.startedAt)} · ${owner(r)}`} />
        ))}

        {q.waiting.map((w) => (
          <Row key={`wait-${w.id}`} icon={<WaitingIcon fontSize="small" />}
               primary={`#${w.position} · ${w.jobName}`}
               secondary={`queued ${ago(w.queuedAt)} · ${owner(w)} · starts on its own`}
               action={w.mine ? (
                 <Tooltip title="Remove from the queue">
                   <IconButton size="small" onClick={() => drop(w.id!)}>
                     <CancelIcon fontSize="small" />
                   </IconButton>
                 </Tooltip>
               ) : undefined} />
        ))}

        {history.length > 0 && (
          <Box sx={{ mt: 1.5, pt: 1.5, borderTop: 1, borderColor: 'divider' }}>
            <Typography variant="caption" color="text.secondary"
                        sx={{ fontWeight: 700 }}>
              RECENTLY QUEUED
            </Typography>
            {history.map((r) => (
              <Row key={`hist-${r.id}`} icon={<HistoryIcon fontSize="small" />}
                   primary={r.jobName}
                   secondary={`${r.status} ${ago(r.finishedAt)} · ${owner(r)}${
                     r.detail ? ` · ${r.detail}` : ''}`}
                   action={<Chip size="small" label={r.status}
                                 color={STATUS_COLOR[r.status || ''] || 'default'}
                                 variant="outlined" />} />
            ))}
          </Box>
        )}
      </CardContent>
    </Card>
  )
}

export default QueuePanel
