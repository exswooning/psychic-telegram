import React from 'react'
import { Box, Chip, Stack, Typography } from '@mui/material'
import { parseSeedUsers } from '@/utils/seedLog'

const STATUS_LABEL = { starting: 'in progress', done: 'done', 'top-up': 'topped up' } as const
const STATUS_COLOR = { starting: 'info', done: 'success', 'top-up': 'success' } as const

/**
 * "Where is the active log of all the users it is touching" -- the raw
 * printed transcript answers that only if you're willing to read past
 * hundreds of interleaved "! label X: HTTP 409" warning lines to find the
 * "[user] starting"/"[user] done" ones. This parses those out (see
 * utils/seedLog.ts) into the actual answer: which users are in flight
 * right now, and what happened to the ones already finished.
 */
const SeedUsersLog: React.FC<{ lines: string[] }> = ({ lines }) => {
  const users = parseSeedUsers(lines)
  if (users.length === 0) return null

  // In-flight first -- that's the answer to "what is it doing right now",
  // the more actionable question while a run is still going. Otherwise
  // keep them in the order seed_sandbox.py printed them.
  const inFlight = users.filter((u) => u.status === 'starting')
  const finished = users.filter((u) => u.status !== 'starting')

  return (
    <Box sx={{ mt: 1 }}>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
        {inFlight.length} in progress · {finished.length} done of {users.length} touched so far
      </Typography>
      <Box sx={{
        maxHeight: 220, overflowY: 'auto', border: '1px solid', borderColor: 'divider',
        borderRadius: 1,
      }}>
        {[...inFlight, ...finished].map((u) => (
          <Stack key={u.email} direction="row" spacing={1} alignItems="flex-start" sx={{
            px: 1.5, py: 0.75, '&:not(:last-of-type)': { borderBottom: '1px solid', borderColor: 'divider' },
          }}>
            <Chip size="small" label={STATUS_LABEL[u.status]} color={STATUS_COLOR[u.status] as any}
                 variant={u.status === 'starting' ? 'filled' : 'outlined'} sx={{ mt: 0.25 }} />
            <Box sx={{ minWidth: 0 }}>
              <Typography variant="body2" sx={{ fontWeight: 600 }} noWrap>{u.email}</Typography>
              {u.detail && (
                <Typography variant="caption" color="text.secondary" sx={{
                  display: 'block', wordBreak: 'break-word',
                }}>
                  {u.detail}
                </Typography>
              )}
            </Box>
          </Stack>
        ))}
      </Box>
    </Box>
  )
}

export default SeedUsersLog
