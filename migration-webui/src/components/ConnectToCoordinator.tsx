/**
 * Join THIS machine to another Bitport, from the browser.
 *
 * The join code already reduced adding a machine to one line, but that line
 * still needs a terminal -- and a machine showing this page does not need
 * one. It can redeem the code itself and write its own node.env.
 *
 * Still an outbound call, made by this machine's own admin: the same
 * request the installer makes. Nothing here lets a coordinator reach in,
 * which is the property the whole node design rests on.
 */
import React, { useState } from 'react'
import {
  Alert, Box, Button, Paper, Stack, TextField, Typography,
} from '@mui/material'
import { Link as LinkIcon } from '@mui/icons-material'
import { connectToCoordinator } from '@/api/controlPlane'

export const ConnectToCoordinator: React.FC = () => {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [ok, setOk] = useState<{ coordinator: string; nodeId: string
                                 accountId: number | null } | null>(null)

  const go = () => {
    setBusy(true); setErr(''); setOk(null)
    // Sent as `command` whatever it is: the server pulls the address and
    // the code out of a pasted one-liner, and a bare code with no address
    // fails there with one message rather than two half-validations.
    connectToCoordinator({ command: value.trim(), code: value.trim() })
      .then(setOk)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false))
  }

  return (
    <Paper variant="outlined" sx={{ p: 2, mt: 3 }} data-testid="connect-panel">
      <Stack direction="row" spacing={1} alignItems="center">
        <LinkIcon fontSize="small" color="action" />
        <Typography variant="subtitle2" sx={{ fontWeight: 700, flex: 1 }}>
          Connect this machine to another coordinator
        </Typography>
        <Button size="small" onClick={() => setOpen((v) => !v)}
                data-testid="toggle-connect">
          {open ? 'Hide' : 'Connect'}
        </Button>
      </Stack>

      {open && (
        <Box sx={{ mt: 2 }}>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            Make this machine a worker for a Bitport running somewhere else.
            Paste the whole join command from that coordinator&apos;s Nodes
            page — no terminal needed.
          </Typography>
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <TextField size="small" fullWidth multiline maxRows={3}
                       label="Join command, or a code"
                       placeholder="irm https://coordinator/api/v2/j/B95B-RZ5Z | iex"
                       value={value} onChange={(e) => setValue(e.target.value)}
                       inputProps={{ 'data-testid': 'connect-input' }} />
            <Button variant="contained" size="small" onClick={go}
                    disabled={busy || !value.trim()}
                    data-testid="connect-submit" sx={{ mt: 0.5 }}>
              {busy ? 'Joining…' : 'Join'}
            </Button>
          </Stack>

          {err && (
            <Alert severity="error" sx={{ mt: 1.5 }} data-testid="connect-error">
              {err}
            </Alert>
          )}
          {ok && (
            <Alert severity="success" sx={{ mt: 1.5 }} data-testid="connect-ok">
              Joined <strong>{ok.coordinator}</strong> as{' '}
              <strong>{ok.nodeId}</strong>
              {ok.accountId ? ` for account ${ok.accountId}` : ''}. Start the
              agent on this machine to pick up work:
              <Box component="pre" sx={{ fontSize: 11, mt: 1, mb: 0,
                                         whiteSpace: 'pre-wrap' }}>
                ./.venv/bin/python node_agent.py
              </Box>
            </Alert>
          )}
        </Box>
      )}
    </Paper>
  )
}

export default ConnectToCoordinator
