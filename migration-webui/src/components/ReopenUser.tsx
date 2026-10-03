import React, { useState } from 'react'
import { Alert, Button, Checkbox, FormControlLabel, Paper, Stack, Typography } from '@mui/material'
import { reopenUser } from '@/api/controlPlane'
import ReasonCodeDialog from './ReasonCodeDialog'

const SERVICES = ['drive', 'gmail', 'calendar', 'contacts', 'tasks', 'chat']

/**
 * Make the next migrate genuinely reattempt this user -- some services, or the
 * whole user (back to PENDING). Before this it was an UPDATE on identity_map typed
 * over SSH. The server refuses while a migration runs on the account, and refuses
 * a service the ledger does not have marked done.
 */
const ReopenUser: React.FC<{ email: string; accountId?: number }> = ({ email, accountId }) => {
  const [picked, setPicked] = useState<string[]>([])
  const [asking, setAsking] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const toggle = (s: string) =>
    setPicked((p) => (p.includes(s) ? p.filter((x) => x !== s) : [...p, s]))

  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="reopen-user">
      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>Run this user again</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        The next migration skips a service its ledger calls done. Reopen the ones it should
        reattempt — or none, to reopen the whole user.
      </Typography>
      <Stack direction="row" sx={{ flexWrap: 'wrap' }}>
        {SERVICES.map((s) => (
          <FormControlLabel key={s} label={s}
            control={<Checkbox size="small" checked={picked.includes(s)} onChange={() => toggle(s)}
                               inputProps={{ 'data-testid': `reopen-${s}` } as never} />} />
        ))}
      </Stack>
      <Button size="small" variant="outlined" onClick={() => setAsking(true)} data-testid="reopen-go">
        {picked.length ? `Reopen ${picked.join(', ')}` : 'Reopen the whole user'}
      </Button>
      {done && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setDone(null)}>{done}</Alert>}
      <ReasonCodeDialog
        open={asking} error={err}
        title={picked.length ? `Reopen ${picked.join(', ')} for ${email}` : `Reopen ${email}`}
        description={<>Nothing is deleted. Items already copied stay mapped and are skipped;
          the next migration reattempts what is missing.</>}
        onCancel={() => { setAsking(false); setErr(null) }}
        onConfirm={async (reason) => {
          try {
            const r = await reopenUser(email, picked, reason, accountId)
            if (!r.ok) throw new Error(r.detail)
            setDone(r.detail); setAsking(false); setErr(null)
          } catch (e) {
            setErr(e instanceof Error ? e.message : String(e))
          }
        }}
      />
    </Paper>
  )
}

export default ReopenUser
