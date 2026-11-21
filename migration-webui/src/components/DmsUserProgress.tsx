import React, { useEffect, useState } from 'react'
import { Box, LinearProgress, Stack, Typography } from '@mui/material'
import { DmsPerUserView, fetchDmsPerUser } from '@/api/controlPlane'

const nf = new Intl.NumberFormat()

/**
 * A bar per user while the DMS imports: each mailbox's message count on the target against the
 * source, read from Gmail itself (the console only reports one total). Counted at most once a
 * minute on the server; an unreadable mailbox says so instead of showing zero.
 */
const DmsUserProgress: React.FC<{ accountId?: number }> = ({ accountId }) => {
  const [data, setData] = useState<DmsPerUserView | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    let live = true
    const load = () => fetchDmsPerUser(accountId)
      .then((d) => { if (live) { setData(d); setErr('') } })
      .catch((e) => { if (live) setErr(String(e?.message || e)) })
    load()
    const t = window.setInterval(load, 60_000)
    return () => { live = false; window.clearInterval(t) }
  }, [accountId])

  if (err) return <Typography variant="body2" color="error">Per-user mail counts: {err}</Typography>
  if (!data) return <Typography variant="body2" color="text.secondary">Counting each mailbox…</Typography>
  if (!data.users.length) return null
  const rows = [...data.users].sort((a, b) => (a.share ?? -1) - (b.share ?? -1))
  return (
    <Box data-testid="dms-user-progress" sx={{ mt: 2 }}>
      <Typography variant="subtitle2">Mail on the target, per user</Typography>
      <Typography variant="caption" color="text.secondary">
        Mailbox totals from Gmail, counted {data.at ? new Date(data.at * 1000).toLocaleTimeString() : 'never'};
        the target also holds Google&apos;s welcome mail, so a few more than the source is normal.
      </Typography>
      <Stack spacing={1} sx={{ mt: 1 }}>
        {rows.map((u) => (
          <Box key={u.source}>
            <Stack direction="row" justifyContent="space-between" spacing={2}>
              <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>{u.source}</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' }}>
                {u.targetCount == null || u.sourceCount == null
                  ? 'could not be read'
                  : `${nf.format(u.targetCount)} of ${nf.format(u.sourceCount)}`}
              </Typography>
            </Stack>
            <LinearProgress variant={u.share == null ? 'indeterminate' : 'determinate'}
              value={u.share == null ? undefined : Math.round(u.share * 100)}
              aria-label={`${u.source}: ${u.share == null ? 'unknown' : Math.round(u.share * 100) + '%'} of its mail on the target`}
              sx={{ height: 6, borderRadius: 1, mt: 0.5 }} />
          </Box>
        ))}
      </Stack>
    </Box>
  )
}

export default DmsUserProgress
