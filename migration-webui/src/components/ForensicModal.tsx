import React, { useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, Divider, Stack, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, Typography,
} from '@mui/material'
import { Replay as RetryIcon } from '@mui/icons-material'
import {
  ForensicDetail, fetchForensics, retryItem,
} from '@/api/controlPlane'
import ReasonCodeDialog from './ReasonCodeDialog'

/**
 * Everything known about one item, so diagnosing a failure stops requiring
 * SSH and a hand-written SQL join. That was the actual workflow during this
 * migration -- every failure investigation ran through
 * `sqlite3 migration.db "select ... from audit_log where ..."`.
 *
 * The one piece of real judgement here is `supersededBySuccess`: a FAILED
 * audit row whose item nonetheless has an `id_mapping` row was already fixed
 * by a later pass. Showing Retry there invites an operator to redo finished
 * work, so the button is withheld and the reason is stated.
 */
interface Props {
  open: boolean
  sourceUser: string | null
  itemId: string | null
  onClose: () => void
  onRetried?: () => void
}

const statusColor = (s: string) =>
  s === 'SUCCESS' ? 'success' : s === 'FAILED' ? 'error' : 'default'

/** Google's errors arrive as one long line with a JSON blob inline. Pulling
 *  the HTTP status and reason out front means the operator sees "403
 *  cannotDeleteResourceWithChildren" instead of scanning 400 characters. */
function summarise(err: string | null): { code: string; reason: string; rest: string } {
  if (!err) return { code: '', reason: '', rest: '' }
  const http = err.match(/HTTP (\d{3})|<HttpError (\d{3})/)
  const reason = err.match(/'reason':\s*'([^']+)'|reason "([^"]+)"/)
  return {
    code: http ? (http[1] ?? http[2]) : '',
    reason: reason ? (reason[1] ?? reason[2]) : '',
    rest: err,
  }
}

const ForensicModal: React.FC<Props> = ({ open, sourceUser, itemId, onClose, onRetried }) => {
  const [data, setData] = useState<ForensicDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [askReason, setAskReason] = useState(false)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || !sourceUser || !itemId) return
    setLoading(true); setLoadError(null); setData(null)
    fetchForensics(sourceUser, itemId)
      .then(setData)
      .catch((e) => setLoadError(e.message))
      .finally(() => setLoading(false))
  }, [open, sourceUser, itemId])

  const doRetry = async (reason: string) => {
    if (!sourceUser || !itemId) return
    setBusy(true); setActionError(null)
    try {
      await retryItem(sourceUser, itemId, reason)
      setAskReason(false)
      onRetried?.()
      onClose()
    } catch (e: any) {
      setActionError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const latest = data?.attempts?.[0]
  const parsed = summarise(latest?.error_message ?? null)

  return (
    <>
      <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
        <DialogTitle>
          <Typography variant="h6">Failure forensics</Typography>
          <Typography variant="caption" color="text.secondary"
                      sx={{ fontFamily: 'ui-monospace, monospace' }}>
            {sourceUser} · {itemId}
          </Typography>
        </DialogTitle>

        <DialogContent dividers>
          {loading && <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress /></Box>}
          {loadError && <Alert severity="error">{loadError}</Alert>}

          {data && (
            <Stack spacing={2}>
              {data.supersededBySuccess && (
                <Alert severity="success">
                  <strong>Already resolved.</strong> This item has a live target
                  mapping, so a later pass copied it successfully. The failed
                  attempt below is history — retrying would redo finished work.
                </Alert>
              )}

              <Box>
                <Typography variant="overline" color="text.secondary">Latest error</Typography>
                {latest?.error_message ? (
                  <>
                    <Stack direction="row" spacing={1} sx={{ my: 1 }}>
                      {parsed.code && <Chip size="small" color="error" label={`HTTP ${parsed.code}`} />}
                      {parsed.reason && <Chip size="small" variant="outlined" label={parsed.reason} />}
                      <Chip size="small" variant="outlined" label={latest.item_type} />
                    </Stack>
                    <Box component="pre" sx={{
                      fontSize: 11, p: 1.5, bgcolor: 'action.hover', borderRadius: 1,
                      overflowX: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                      maxHeight: 220, m: 0,
                    }}>
                      {parsed.rest}
                    </Box>
                  </>
                ) : (
                  <Typography variant="body2" color="text.secondary">
                    No error recorded on the most recent attempt.
                  </Typography>
                )}
              </Box>

              <Divider />

              <Box>
                <Typography variant="overline" color="text.secondary">
                  Attempt history ({data.attempts.length})
                </Typography>
                <TableContainer sx={{ overflowX: 'auto' }}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>When</TableCell>
                      <TableCell>Type</TableCell>
                      <TableCell>Status</TableCell>
                      <TableCell align="right">Bytes</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {data.attempts.map((a) => (
                      <TableRow key={a.id}>
                        <TableCell sx={{ whiteSpace: 'nowrap' }}>
                          {new Date(a.timestamp).toLocaleString()}
                        </TableCell>
                        <TableCell>{a.item_type}</TableCell>
                        <TableCell>
                          <Chip size="small" label={a.status}
                                color={statusColor(a.status) as any} variant="outlined" />
                        </TableCell>
                        <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                          {a.bytes_moved?.toLocaleString() ?? 0}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                </TableContainer>
              </Box>

              <Divider />

              <Stack direction="row" spacing={4} flexWrap="wrap">
                <Box>
                  <Typography variant="overline" color="text.secondary">Target mapping</Typography>
                  <Typography variant="body2" sx={{ fontFamily: 'ui-monospace, monospace' }}>
                    {data.mapping
                      ? `${data.mapping.type} → ${data.mapping.target_id}`
                      : 'none — never landed on the target'}
                  </Typography>
                </Box>
                <Box>
                  <Typography variant="overline" color="text.secondary">User</Typography>
                  <Typography variant="body2">
                    {data.identity
                      ? `${data.identity.target_email} (${data.identity.status})`
                      : 'not in identity_map'}
                  </Typography>
                </Box>
              </Stack>
            </Stack>
          )}
        </DialogContent>

        <DialogActions sx={{ px: 3, py: 2 }}>
          <Button onClick={onClose}>Close</Button>
          <Button
            variant="contained" startIcon={<RetryIcon />}
            disabled={!data || data.supersededBySuccess}
            onClick={() => setAskReason(true)}
          >
            Fix &amp; Retry
          </Button>
        </DialogActions>
      </Dialog>

      <ReasonCodeDialog
        open={askReason}
        title="Retry this item"
        busy={busy}
        error={actionError}
        description={
          <>
            Clears the FAILED audit row for <code>{itemId}</code> and runs a
            <strong> delta pass</strong> scoped to {sourceUser}. Delta rather
            than migrate, because migrate skips any user already marked DONE —
            which would silently no-op.
          </>
        }
        onCancel={() => { setAskReason(false); setActionError(null) }}
        onConfirm={doRetry}
      />
    </>
  )
}

export default ForensicModal
