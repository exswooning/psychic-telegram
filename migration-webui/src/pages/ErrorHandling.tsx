import React, { useCallback, useEffect, useState } from 'react'
import {
  Box,
  Typography,
  Card,
  CardContent,
  Stack,
  Chip,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Button,
  TextField,
  IconButton,
  Tooltip,
} from '@mui/material'
import { Refresh as RefreshIcon } from '@mui/icons-material'
import { fetchFailures, FailureRow } from '@/api/controlPlane'
import ForensicModal from '@/components/ForensicModal'

/**
 * A dedicated, filterable failures triage view -- distinct from Mission
 * Control's own "Recent failures" section, which is a short scrollable
 * list meant for watching a run live, not for working through a large
 * backlog. Same real data (fetchFailures/ForensicModal), just room to
 * group by error type and search across everything on file.
 */
const ErrorHandling: React.FC = () => {
  const [failures, setFailures] = useState<FailureRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const [forensic, setForensic] = useState<{ user: string; item: string } | null>(null)

  const refresh = useCallback(() => {
    setLoading(true); setError(null)
    fetchFailures()
      .then(setFailures)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [])

  // Auto-poll, not fetch-once-on-mount -- a page left open while a job is
  // actively failing/retrying items showed the snapshot from whenever it
  // was first opened, forever, until someone happened to click Re-check.
  // Same 5s cadence Jobs.tsx/RunningNow.tsx already use.
  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 5000)
    return () => clearInterval(id)
  }, [refresh])

  const needle = filter.trim().toLowerCase()
  const visible = needle
    ? failures.filter((f) =>
        f.source_user.toLowerCase().includes(needle)
        || f.item_type.toLowerCase().includes(needle)
        || (f.error_message ?? '').toLowerCase().includes(needle))
    : failures

  // Counted, not filtered out: blocked still belongs on this page,
  // it just is not a failure.
  const blockedCount = failures.filter((f) => f.status === 'BLOCKED').length

  const byType = new Map<string, number>()
  for (const f of failures) byType.set(f.item_type, (byType.get(f.item_type) ?? 0) + 1)

  return (
    <Box>
      <Stack direction="row" alignItems="center" sx={{ mb: 0.5 }}>
        <Typography variant="h4" sx={{ fontWeight: 700, flexGrow: 1 }}>Failures</Typography>
        <Tooltip title="Re-check">
          <span>
            <IconButton size="small" onClick={refresh} disabled={loading}>
              <RefreshIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Every FAILED item on file, across every user. Click a row for the
        full attempt history and a scoped retry.
      </Typography>

      {error && (
        <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'error.main', mb: 3 }}>
          <CardContent>
            <Typography variant="body2" color="error">{error}</Typography>
          </CardContent>
        </Card>
      )}

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardContent>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>
            By type ({failures.length} total
            {blockedCount > 0 && `, ${blockedCount} blocked`})
          </Typography>
          {blockedCount > 0 && (
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              Blocked rows are not failures -- they are waiting on something
              outside this tool, usually a Workspace licence. They retry on
              their own once that is resolved; nothing here needs a re-run.
            </Typography>
          )}
          {byType.size > 0 ? (
            <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 1 }}>
              {[...byType.entries()].sort((a, b) => b[1] - a[1]).map(([type, count]) => (
                <Chip key={type} label={`${type} · ${count}`} size="small"
                      color="error" variant="outlined"
                      onClick={() => setFilter(type)} />
              ))}
            </Stack>
          ) : (
            <Typography variant="body2" color="text.secondary">
              {loading ? 'Checking…' : 'No failures recorded.'}
            </Typography>
          )}
        </CardContent>
      </Card>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <CardContent>
          <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 2 }}>
            <Typography variant="h6" sx={{ fontWeight: 600, flexGrow: 1 }}>
              All failures {needle && `(${visible.length} matching)`}
            </Typography>
            <TextField size="small" placeholder="Filter by user, type, or message"
                       value={filter} onChange={(e) => setFilter(e.target.value)}
                       sx={{ width: 320 }} />
            {filter && <Button size="small" onClick={() => setFilter('')}>Clear</Button>}
          </Stack>
          <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 520 }}>
            <Table stickyHeader size="small">
              <TableHead>
                <TableRow>
                  <TableCell sx={{ fontWeight: 600 }}>Time</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>User</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Type</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Error</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {visible.map((f) => (
                  <TableRow key={f.id} hover sx={{ cursor: 'pointer' }}
                            onClick={() => setForensic({ user: f.source_user, item: f.item_id })}>
                    <TableCell sx={{ whiteSpace: 'nowrap' }}>
                      {new Date(f.timestamp).toLocaleString()}
                    </TableCell>
                    <TableCell>{f.source_user}</TableCell>
                    <TableCell>
                      <Stack direction="row" spacing={0.5} alignItems="center">
                        <Chip size="small" variant="outlined"
                              color={f.status === 'BLOCKED' ? 'warning' : 'error'}
                              label={f.item_type} />
                        {f.status === 'BLOCKED' && (
                          <Chip size="small" color="warning" variant="filled"
                                data-testid={`blocked-${f.id}`}
                                label="waiting on you" />
                        )}
                      </Stack>
                    </TableCell>
                    <TableCell sx={{ maxWidth: 480 }}>
                      <Typography variant="body2" noWrap title={f.error_message ?? ''}>
                        {f.error_message ?? '(no message)'}
                      </Typography>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
          {!loading && visible.length === 0 && (
            <Typography variant="body2" color="text.secondary" sx={{ py: 3, textAlign: 'center' }}>
              {failures.length === 0 ? 'No failures recorded.' : `Nothing matches "${filter}".`}
            </Typography>
          )}
        </CardContent>
      </Card>

      <ForensicModal
        open={!!forensic} sourceUser={forensic?.user ?? null} itemId={forensic?.item ?? null}
        onClose={() => setForensic(null)} onRetried={refresh}
      />
    </Box>
  )
}

export default ErrorHandling
