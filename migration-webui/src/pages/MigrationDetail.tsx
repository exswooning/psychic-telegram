import React, { useCallback, useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  Alert, Box, Button, Chip, CircularProgress, IconButton, Paper, Stack,
  Table, TableBody, TableCell, TableHead, TableRow, Typography,
} from '@mui/material'
import {
  Refresh as RefreshIcon, ArrowBack as BackIcon,
  ArrowForward as ArrowIcon,
} from '@mui/icons-material'
import { fetchMigrationDetail, MigrationDetail as Detail } from '@/api/controlPlane'

/**
 * One migration in full: what moved, what failed, and why.
 *
 * Failures are grouped by cause, not listed per item. A run that fails 50
 * contacts fails them for ONE reason, and fifty identical HTTP 400s scrolled
 * down a page hides that completely -- the count and one example are what
 * anybody acts on. Affected mailboxes are named per cause because "which
 * users" is the next question every single time.
 */

const Stat: React.FC<{ id: string; label: string; value: number; tone?: 'error' }> =
  ({ id, label, value, tone }) => (
    <Box data-testid={`stat-${id}`}>
      <Typography sx={{ fontWeight: 700, fontSize: 22, lineHeight: 1.2,
                        fontVariantNumeric: 'tabular-nums',
                        color: tone === 'error' && value > 0
                          ? 'error.main' : 'text.primary' }}>
        {value.toLocaleString()}
      </Typography>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
    </Box>
  )

export const MigrationDetail: React.FC = () => {
  const { accountId } = useParams()
  const navigate = useNavigate()
  const [d, setD] = useState<Detail | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const id = Number(accountId)

  const refresh = useCallback(() => {
    if (!Number.isFinite(id)) return
    setLoading(true)
    fetchMigrationDetail(id)
      .then((r) => { setD(r); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [id])

  useEffect(() => {
    refresh()
    const t = window.setInterval(refresh, 5000)
    return () => window.clearInterval(t)
  }, [refresh])

  const p = d?.progress

  return (
    <Box sx={{ p: 3 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 2 }}>
        <Button size="small" startIcon={<BackIcon />}
                onClick={() => navigate('/migrations')}
                data-testid="back">
          Migrations
        </Button>
        {loading && <CircularProgress size={16} />}
        <Box sx={{ flex: 1 }} />
        <IconButton size="small" onClick={refresh} aria-label="refresh">
          <RefreshIcon fontSize="small" />
        </IconButton>
      </Stack>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {d?.error && <Alert severity="warning" sx={{ mb: 2 }}>{d.error}</Alert>}

      {d && (
        <>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 2 }}>
            <Typography variant="h5" sx={{ fontWeight: 700 }}>
              {d.sourceDomain || '—'}
            </Typography>
            <ArrowIcon color="disabled" />
            <Typography variant="h5" sx={{ fontWeight: 700 }}>
              {d.targetDomain || '—'}
            </Typography>
            <Chip size="small" label={d.running ? 'running' : 'idle'}
                  color={d.running ? 'primary' : 'default'}
                  variant={d.running ? 'filled' : 'outlined'} />
          </Stack>

          {p && (
            <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
              <Stack direction="row" spacing={4} sx={{ flexWrap: 'wrap', gap: 2 }}>
                <Stat id="users" label="users" value={p.users} />
                <Stat id="done" label="done" value={p.done} />
                <Stat id="running" label="running" value={p.running} />
                <Stat id="pending" label="pending" value={p.pending} />
                <Stat id="failed" label="users failed" value={p.failed} tone="error" />
                <Stat id="items" label="items migrated" value={p.items} />
                <Stat id="itemsfailed" label="items failed" value={p.itemsFailed}
                      tone="error" />
              </Stack>
            </Paper>
          )}

          <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
              What moved
            </Typography>
            {d.items.length === 0 ? (
              <Typography variant="body2" color="text.secondary"
                          data-testid="nothing-moved">
                Nothing migrated yet for this pair.
              </Typography>
            ) : (
              <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 0.5 }}>
                {d.items.map((it) => (
                  <Chip key={it.type} size="small" variant="outlined"
                        data-testid={`item-${it.type}`}
                        label={`${it.type} · ${it.count.toLocaleString()}`} />
                ))}
              </Stack>
            )}
          </Paper>

          {d.failedUsers.length > 0 && (
            <Paper variant="outlined" sx={{ p: 2, mb: 3 }}
                   data-testid="failed-users">
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
                Users that did not migrate ({d.failedUsers.length})
              </Typography>
              <Stack spacing={1.5}>
                {d.failedUsers.map((u) => (
                  <Box key={u.sourceUser} data-testid={`faileduser-${u.sourceUser}`}>
                    <Typography variant="body2" sx={{ fontWeight: 600 }}>
                      {u.sourceUser}
                    </Typography>
                    <Typography variant="caption" color="text.secondary"
                                sx={{ whiteSpace: 'pre-wrap' }}>
                      {u.detail || 'no detail recorded'}
                    </Typography>
                  </Box>
                ))}
              </Stack>
            </Paper>
          )}

          <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="users-table">
            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
              Users ({d.users.length.toLocaleString()})
            </Typography>
            {/* Failures first. A 200-row table sorted alphabetically buries
                the two rows anybody opened this page to find. */}
            <Box sx={{ maxHeight: 420, overflowY: 'auto' }}>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    <TableCell>user</TableCell>
                    <TableCell>target</TableCell>
                    <TableCell>state</TableCell>
                    <TableCell>services done</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {d.users.map((u) => (
                    <TableRow key={u.sourceUser}
                              data-testid={`user-${u.sourceUser}`}>
                      <TableCell sx={{ fontSize: 12 }}>{u.sourceUser}</TableCell>
                      <TableCell sx={{ fontSize: 12 }}>{u.targetUser}</TableCell>
                      <TableCell>
                        <Chip size="small"
                              variant={u.status === 'DONE' ? 'outlined' : 'filled'}
                              color={u.status === 'DONE' ? 'success'
                                     : u.status === 'FAILED' ? 'error'
                                     : u.status === 'RUNNING' ? 'primary' : 'default'}
                              label={u.status.toLowerCase()} />
                      </TableCell>
                      <TableCell sx={{ fontSize: 11 }}>
                        {u.services || '—'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          </Paper>

          <Paper variant="outlined" sx={{ p: 2 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
              Errors, grouped by cause
            </Typography>
            {d.failures.length === 0 ? (
              <Typography variant="body2" color="text.secondary"
                          data-testid="no-failures">
                No failures recorded.
              </Typography>
            ) : (
              <Box sx={{ overflowX: 'auto' }}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>count</TableCell>
                      <TableCell>type</TableCell>
                      <TableCell>cause</TableCell>
                      <TableCell>affected</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {d.failures.map((f, i) => (
                      <TableRow key={`${f.itemType}-${i}`}
                                data-testid={`failure-${i}`}>
                        <TableCell sx={{ fontWeight: 700 }}>
                          {f.count.toLocaleString()}
                        </TableCell>
                        <TableCell sx={{ fontSize: 12 }}>{f.itemType}</TableCell>
                        <TableCell sx={{ fontSize: 12, maxWidth: 520 }}>
                          {f.reason}
                        </TableCell>
                        <TableCell sx={{ fontSize: 11 }}>
                          {f.users.join(', ')}
                          {f.users.length >= 5 && ' …'}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            )}
          </Paper>
        </>
      )}
    </Box>
  )
}

export default MigrationDetail
