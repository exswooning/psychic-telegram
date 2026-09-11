/**
 * Start and stop the work a tenant's nodes do.
 *
 * The button does not reach into anything. It writes desired state on the
 * coordinator; each node asks for it on its own poll and acts. Same shape
 * as a Kubernetes node reading its spec, and for the same reason given in
 * fleet_agent.py: a control plane that could reach into its nodes would
 * need credentials for every machine holding service-account keys for both
 * tenants, which turns a dashboard into a lateral-movement path.
 *
 * That indirection is visible on purpose. "Nodes will pick this up within
 * their poll" is the truth, and a button that implied instant remote
 * control would be lying about what just happened.
 */
import React, { useState } from 'react'
import {
  Alert, Box, Button, Paper, Stack, TextField, Typography,
} from '@mui/material'
import {
  PlayArrow as StartIcon, Stop as StopIcon,
} from '@mui/icons-material'
import { setNodeDirective } from '@/api/controlPlane'

export const NodeWorkSwitch: React.FC<{ accountId?: number }> = ({ accountId }) => {
  const [services, setServices] = useState('gmail')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [state, setState] = useState<'started' | 'stopped' | null>(null)

  const send = (run: boolean) => {
    if (accountId === undefined) return
    setBusy(true); setErr('')
    setNodeDirective(accountId, run, run ? services : '')
      .then(() => setState(run ? 'started' : 'stopped'))
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false))
  }

  return (
    <Paper variant="outlined" sx={{ p: 2, mt: 3 }} data-testid="work-switch">
      <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
        Node work
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        Tells every node on this tenant to start or stop migrating. Nodes
        pick it up on their next poll — this page never connects to a
        machine, machines connect to it.
      </Typography>
      <Stack direction="row" spacing={1} alignItems="center"
             sx={{ flexWrap: 'wrap', gap: 1 }}>
        <TextField size="small" label="Services" value={services}
                   onChange={(e) => setServices(e.target.value)}
                   sx={{ width: 200 }}
                   inputProps={{ 'data-testid': 'work-services' }} />
        <Button variant="contained" size="small" startIcon={<StartIcon />}
                disabled={busy || accountId === undefined}
                onClick={() => send(true)} data-testid="work-start">
          Start
        </Button>
        <Button variant="outlined" color="warning" size="small"
                startIcon={<StopIcon />} disabled={busy || accountId === undefined}
                onClick={() => send(false)} data-testid="work-stop">
          Stop
        </Button>
      </Stack>

      {state === 'started' && (
        <Alert severity="success" sx={{ mt: 1.5 }} data-testid="work-started">
          Nodes on this tenant will start migrating {services || 'every service'}{' '}
          within their poll interval. A node that is not running{' '}
          <code>node_agent.py</code> will not notice.
        </Alert>
      )}
      {state === 'stopped' && (
        <Alert severity="info" sx={{ mt: 1.5 }} data-testid="work-stopped">
          Nodes will stop after finishing the user each is on. Interrupting
          mid-user is worse: it strands a mailbox half-copied, and on Drive
          leaves items the local ledger never recorded.
        </Alert>
      )}
      {err && <Alert severity="error" sx={{ mt: 1.5 }} data-testid="work-error">{err}</Alert>}
      {accountId === undefined && (
        <Box sx={{ mt: 1.5 }}>
          <Alert severity="info">Pick a tenant above first.</Alert>
        </Box>
      )}
    </Paper>
  )
}

export default NodeWorkSwitch
