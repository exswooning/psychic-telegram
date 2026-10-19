import React, { useCallback, useEffect, useState } from 'react'
import { Alert, Box, Button, Chip, Stack, Typography } from '@mui/material'
import { ExactCopy, fetchExactCopy, setExactCopy } from '@/api/controlPlane'
import ReasonCodeDialog from './ReasonCodeDialog'

/** A government source is copied by download and upload unless the operator switches
 *  exact (server-side) copies on. The grant only changes when the source is set up again. */
const GovExactCopy: React.FC = () => {
  const [state, setState] = useState<ExactCopy | null>(null)
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState('')
  const load = useCallback(() => { fetchExactCopy().then(setState).catch(() => setState(null)) }, [])
  useEffect(load, [load])
  if (!state?.government) return null
  const turnOn = !state.on

  return (
    <Box sx={{ mt: 2, p: 1.5, border: '1px solid', borderColor: 'divider', borderRadius: 1 }}
         data-testid="gov-exact-copy">
      <Stack direction="row" spacing={1} alignItems="center">
        <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>Exact copies (server-side)</Typography>
        <Chip size="small" label={state.on ? 'On' : 'Off'} color={state.on ? 'warning' : 'default'} />
        <Box sx={{ flex: 1 }} />
        <Button size="small" variant="outlined" onClick={() => { setError(null); setAsking(true) }}
                data-testid="gov-exact-copy-toggle">
          {turnOn ? 'Turn on' : 'Turn off'}
        </Button>
      </Stack>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
        {state.on
          ? `Google copies each file exactly. ${state.domain}'s grant includes Drive write while this is on; Bitport's own guard allows only a copy into its holding drive. Turn off, then set the source up again, to go back to read-only.`
          : `Files go by download and upload, and Google refuses every write to ${state.domain}. Turning this on needs the source set up again, adding Drive write to its grant.`}
      </Typography>
      {done && <Alert severity="info" sx={{ mt: 1 }}>{done}</Alert>}
      <ReasonCodeDialog
        open={asking}
        title={turnOn ? 'Turn on exact copies' : 'Turn off exact copies'}
        description={turnOn
          ? `Then set up ${state.domain} again: its grant gains Drive write, so Google no longer refuses writes and only Bitport's own guard holds the source to reads plus a copy into its holding drive.`
          : `Then set up ${state.domain} again: its grant is rewritten read-only and Google refuses every write again.`}
        busy={busy}
        error={error}
        onCancel={() => setAsking(false)}
        onConfirm={(reason) => {
          setBusy(true)
          setExactCopy(turnOn, reason)
            .then((r) => { setDone(r.detail); setAsking(false); load() })
            .catch((e) => setError(e instanceof Error ? e.message : String(e)))
            .finally(() => setBusy(false))
        }}
      />
    </Box>
  )
}

export default GovExactCopy
