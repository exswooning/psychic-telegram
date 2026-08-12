import React, { useState } from 'react'
import {
  Alert, AlertTitle, Box, Button, Chip, CircularProgress, Collapse, Stack,
  Table, TableBody, TableCell, TableHead, TableRow, Typography,
} from '@mui/material'
import {
  GppMaybe as ShieldIcon, ExpandMore, ExpandLess,
} from '@mui/icons-material'
import { PublicShare, revertPublicShares } from '@/api/controlPlane'
import ReasonCodeDialog from './ReasonCodeDialog'

/**
 * The security kill switch, and the dashboard that justifies it.
 *
 * This is not hypothetical. link_flip's restore step tried to re-create the
 * `owner` permission, Drive rejects that with 403, every restore failed, and
 * **93 files were left publicly link-shareable on the live target tenant**.
 * Nothing surfaced it — it took a manual ACL audit to find them, hours later.
 *
 * So the component is built around one question, answered without clicking:
 * *is anything public right now?* When the answer is no it stays quiet and
 * green. When the answer is yes it is loud, lists the files, and offers one
 * button that revokes every `anyone` grant on the tenant.
 */
interface Props {
  shares: PublicShare[]
  /** Live count from the WS feed. May lead `shares` by a tick, so it is
   *  shown rather than derived from the array length. */
  liveCount?: number
  onReverted?: () => void
}

const EmergencyBrake: React.FC<Props> = ({ shares, liveCount, onReverted }) => {
  const [askReason, setAskReason] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)

  const count = liveCount ?? shares.length
  const exposed = count > 0

  const doRevert = async (reason: string) => {
    setBusy(true); setError(null)
    try {
      const r = await revertPublicShares(reason, 'target')
      setResult(r.detail)
      setAskReason(false)
      onReverted?.()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Alert
        severity={exposed ? 'error' : 'success'}
        variant={exposed ? 'filled' : 'outlined'}
        icon={<ShieldIcon />}
        sx={{ borderRadius: 2 }}
        action={
          exposed ? (
            <Button
              color="inherit" variant="outlined" size="small"
              onClick={() => setAskReason(true)}
              sx={{ borderColor: 'currentColor', fontWeight: 700 }}
            >
              Revert all public shares
            </Button>
          ) : undefined
        }
      >
        <AlertTitle sx={{ fontWeight: 700 }}>
          {exposed
            ? `${count} file${count === 1 ? '' : 's'} publicly shared on the target tenant`
            : 'No public shares detected on the target tenant'}
        </AlertTitle>
        {exposed ? (
          <>
            Anyone with the link can read {count === 1 ? 'this file' : 'these files'}.
            Reverting revokes every <code>anyone</code> grant on the target.
            <Button
              size="small" color="inherit" sx={{ ml: 1 }}
              endIcon={expanded ? <ExpandLess /> : <ExpandMore />}
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? 'Hide' : 'Show'} files
            </Button>
          </>
        ) : (
          <Typography variant="body2">
            Monitored continuously. A grant appearing here triggers a
            CRITICAL_ALERT on the live feed.
          </Typography>
        )}
      </Alert>

      <Collapse in={expanded && exposed}>
        <Box sx={{ mt: 1, border: '1px solid', borderColor: 'divider', borderRadius: 2,
                   maxHeight: 300, overflowY: 'auto', overflowX: 'auto' }}>
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                <TableCell>User</TableCell>
                <TableCell>File</TableCell>
                <TableCell>Grant</TableCell>
                <TableCell>Detected</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {shares.map((s) => (
                <TableRow key={s.id}>
                  <TableCell>{s.user_email}</TableCell>
                  <TableCell sx={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>
                    {s.file_name ?? s.file_id}
                  </TableCell>
                  <TableCell>
                    <Chip size="small" color="error" variant="outlined"
                          label={`${s.grant_type}:${s.role}`} />
                  </TableCell>
                  <TableCell sx={{ whiteSpace: 'nowrap' }}>
                    {new Date(s.detected_at).toLocaleString()}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>
      </Collapse>

      {result && (
        <Alert severity="info" sx={{ mt: 1 }} onClose={() => setResult(null)}>
          {result}
        </Alert>
      )}
      {busy && (
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1 }}>
          <CircularProgress size={16} />
          <Typography variant="caption">Revoking…</Typography>
        </Stack>
      )}

      <ReasonCodeDialog
        open={askReason}
        destructive
        confirmPhrase="REVERT"
        busy={busy}
        error={error}
        title="Revert all public shares"
        description={
          <>
            Revokes <strong>every <code>anyone</code> permission</strong> on the
            target tenant ({count} known{count ? ', possibly more' : ''}).
            This is not selective and cannot be undone from here — any share
            that was <em>intentionally</em> public will also be revoked and must
            be re-created by hand.
          </>
        }
        onCancel={() => { setAskReason(false); setError(null) }}
        onConfirm={doRevert}
      />
    </>
  )
}

export default EmergencyBrake
