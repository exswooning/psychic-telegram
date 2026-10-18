/**
 * A sign-in the server could not finish, finished on this device instead.
 *
 * Google challenges the server's browser (an unfamiliar device, abroad) -- live, it
 * asked it for a phone number. The operator's own browser is usually already signed
 * in as that admin, so full_setup hands the two steps over: the gcloud sign-in (open
 * the link, paste back the code Google shows) and the delegation grant (a pre-filled
 * Admin console link; the setup notices on its own when it is live).
 */
import React, { useState } from 'react'
import { Alert, Box, Button, Stack, TextField, Typography } from '@mui/material'
import { FullSetupStatus, sendFullSetupCode } from '@/api/controlPlane'

export const DeviceHandoff: React.FC<{
  side: 'source' | 'target'
  handoff: FullSetupStatus['handoff']
}> = ({ side, handoff }) => {
  const [code, setCode] = useState('')
  const [sent, setSent] = useState('')
  const [busy, setBusy] = useState(false)
  if (!handoff) return null
  const signin = handoff.kind === 'signin'
  return (
    <Alert severity="info" sx={{ mb: 2 }} data-testid={`handoff-${side}`}>
      <Typography sx={{ fontWeight: 600, mb: 0.5 }}>
        {signin ? 'Finish signing in on this device' : 'Authorise delegation on this device'}
      </Typography>
      <Typography variant="body2" sx={{ mb: 1.5 }}>
        {signin
          ? 'Google kept challenging the sign-in on the server. Open the link here, where the admin is already signed in, allow it, then paste the code Google shows you.'
          : 'Open the link here, where the admin is signed in, and press Authorise. Setup carries on by itself once Google says the grant is live.'}
      </Typography>
      <Button variant="contained" size="small" href={handoff.url} target="_blank"
              rel="noopener noreferrer" data-testid={`handoff-${side}-open`}>
        {signin ? 'Sign in on this device' : 'Open the Admin console'}
      </Button>
      {signin ? (
        <Stack direction="row" spacing={1} sx={{ mt: 1.5 }} alignItems="center">
          <TextField size="small" label="Code from Google" value={code} sx={{ flex: 1 }}
                     inputProps={{ 'data-testid': `handoff-${side}-code` }}
                     onChange={(e) => setCode(e.target.value.trim())} />
          <Button variant="outlined" size="small" disabled={code.length < 4 || busy}
                  data-testid={`handoff-${side}-send`}
                  onClick={() => {
                    setBusy(true)
                    sendFullSetupCode(side, code)
                      .then((r) => setSent(r.ok ? 'Sent. Setup continues on its own.' : (r.detail || 'Not sent')))
                      .catch((e) => setSent(String(e)))
                      .finally(() => setBusy(false))
                  }}>
            Send
          </Button>
        </Stack>
      ) : (
        <Box sx={{ mt: 1.5 }}>
          <Typography variant="caption" sx={{ display: 'block' }}>
            If the form opens empty: Add new, then paste these.
          </Typography>
          <TextField size="small" fullWidth label="Client ID" value={handoff.clientId ?? ''}
                     sx={{ mt: 1 }} InputProps={{ readOnly: true }} />
          <TextField size="small" fullWidth multiline maxRows={3} label="OAuth scopes"
                     value={handoff.scopes ?? ''} sx={{ mt: 1 }} InputProps={{ readOnly: true }} />
        </Box>
      )}
      {sent && <Typography variant="body2" sx={{ mt: 1 }}>{sent}</Typography>}
    </Alert>
  )
}

export default DeviceHandoff
