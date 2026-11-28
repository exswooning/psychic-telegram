/**
 * A sign-in the server could not finish, finished on this device instead.
 *
 * Google challenges the server's browser (an unfamiliar device, abroad) -- live, it
 * asked it for a phone number. The operator's own browser is usually already signed
 * in as that admin, so full_setup hands the two steps over: the gcloud sign-in (open
 * the link, paste back the code Google shows) and the delegation grant (a pre-filled
 * Admin console link; the setup notices on its own when it is live).
 */
import React, { useEffect, useRef, useState } from 'react'
import { Alert, Box, Button, Checkbox, FormControlLabel, Stack, TextField, Typography } from '@mui/material'
import { FullSetupStatus, sendFullSetupCode, sendFullSetupNotice } from '@/api/controlPlane'

export const DeviceHandoff: React.FC<{
  side: 'source' | 'target'
  handoff: FullSetupStatus['handoff']
}> = ({ side, handoff }) => {
  const [code, setCode] = useState('')
  const [sent, setSent] = useState('')
  const [busy, setBusy] = useState(false)
  const [reviewed, setReviewed] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [clock, setClock] = useState(Date.now())
  const identity = `${handoff?.kind || ''}:${handoff?.requestId || handoff?.url || ''}`
  const latestIdentity = useRef(identity)
  latestIdentity.current = identity
  useEffect(() => {
    setCode(''); setSent(''); setBusy(false); setReviewed(false); setSubmitted(false); setClock(Date.now())
  }, [identity, handoff?.kind])
  useEffect(() => {
    if (!handoff?.expiresAt) return
    const timer = setInterval(() => setClock(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [handoff?.expiresAt])
  if (!handoff) return null
  if (handoff.kind === 'review') {
    const expired = !handoff.expiresAt || clock >= handoff.expiresAt * 1000
    const valid = /^[a-f0-9]{48}$/.test(handoff.requestId || '') &&
      /^[a-f0-9]{64}$/.test(handoff.noticeHash || '') && !!handoff.adminEmail
    const respond = (decision: 'approve' | 'decline') => {
      setBusy(true)
      const respondingTo = identity
      sendFullSetupNotice(side, handoff.requestId!, handoff.noticeHash!, decision)
        .then(r => {
          if (latestIdentity.current !== respondingTo) return
          setSent(r.detail || (r.ok ? 'Response recorded.' : 'Not submitted')); setSubmitted(r.ok)
        })
        .catch(e => { if (latestIdentity.current === respondingTo) setSent(String(e)) })
        .finally(() => { if (latestIdentity.current === respondingTo) setBusy(false) })
    }
    return <Alert severity="warning" sx={{ mb: 2 }} data-testid={`handoff-${side}`}>
      <Typography fontWeight={600}>{handoff.title || 'Google setup needs your review'}</Typography>
      <Typography variant="body2" sx={{ mt: 1 }}>Account: <strong>{handoff.adminEmail}</strong> · {side} tenant</Typography>
      <Typography variant="body2" sx={{ mt: 1 }}>
        {handoff.category === 'cloud_sdk_access'
          ? 'This grants Google Cloud SDK the Cloud access listed below so setup can create the migration project and credentials.'
          : handoff.category === 'cloud_console_terms'
            ? 'This accepts the displayed Google Cloud Platform and applicable service terms for this account. Review the selected country and terms below. Email promotions are not selected.'
          : 'This acknowledges Google’s account terms and your organization administrator’s access to account data.'}
        {' '}Review the notice before continuing. Approval applies once to this account, this screen and this setup run.
      </Typography>
      <Box component="pre" sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 240,
        overflow: 'auto', fontFamily: 'inherit', fontSize: 13, bgcolor: 'action.hover', p: 1.5, borderRadius: 1 }}>
        {handoff.text || 'Notice unavailable. Restart setup to capture it again.'}
      </Box>
      <FormControlLabel control={<Checkbox checked={reviewed} disabled={expired || submitted || busy || !valid}
        onChange={e => setReviewed(e.target.checked)} />}
        label={`I reviewed and approve this Google step for ${handoff.adminEmail || 'this account'}`} />
      <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
        <Button variant="contained" disabled={!reviewed || expired || submitted || busy || !valid}
          onClick={() => respond('approve')}>Approve and continue</Button>
        <Button disabled={expired || submitted || busy || !valid}
          onClick={() => respond('decline')}>Decline this step</Button>
      </Stack>
      <Typography variant="caption" display="block" sx={{ mt: 1 }}>
        {expired ? 'This request expired. Restart setup to obtain a fresh review.'
          : submitted ? 'Response sent. Waiting for Google to confirm progress.'
            : 'Setup is waiting for your decision; no approval button is clicked automatically.'}
      </Typography>
      {sent && <Typography variant="body2" sx={{ mt: 1 }}>{sent}</Typography>}
    </Alert>
  }
  const signin = handoff.kind === 'signin'
  const signinExpired = signin && !!handoff.expiresAt && clock >= handoff.expiresAt * 1000
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
      <Button component="a" variant="contained" size="small" href={handoff.url} target="_blank" disabled={signinExpired || !handoff.url}
              rel="noopener noreferrer" data-testid={`handoff-${side}-open`}>
        {signin ? 'Sign in on this device' : 'Open the Admin console'}
      </Button>
      {signin ? (
        <Stack direction="row" spacing={1} sx={{ mt: 1.5 }} alignItems="center">
          <TextField size="small" label="Code from Google" value={code} sx={{ flex: 1 }}
                     inputProps={{ 'data-testid': `handoff-${side}-code` }}
                     onChange={(e) => setCode(e.target.value.trim())} />
          <Button variant="outlined" size="small" disabled={code.length < 4 || busy || signinExpired}
                  data-testid={`handoff-${side}-send`}
                  onClick={() => {
                    setBusy(true)
                    const respondingTo = identity
                    sendFullSetupCode(side, code)
                      .then((r) => { if (latestIdentity.current === respondingTo) setSent(r.ok ? 'Sent. Setup continues on its own.' : (r.detail || 'Not sent')) })
                      .catch((e) => { if (latestIdentity.current === respondingTo) setSent(String(e)) })
                      .finally(() => { if (latestIdentity.current === respondingTo) setBusy(false) })
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
      {signinExpired && <Typography variant="body2" sx={{ mt: 1 }}>This sign-in expired. Restart setup for a fresh Google link; old codes cannot resume it.</Typography>}
      {sent && <Typography variant="body2" sx={{ mt: 1 }}>{sent}</Typography>}
    </Alert>
  )
}

export default DeviceHandoff
