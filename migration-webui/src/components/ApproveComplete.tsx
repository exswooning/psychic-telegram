import React, { useEffect, useState } from 'react'
import { Alert, Button, Chip, Paper, Stack, Typography } from '@mui/material'
import { approveMigrationComplete, fetchGcloudIdentities, GcloudIdentity } from '@/api/controlPlane'
import ReasonCodeDialog from './ReasonCodeDialog'

/**
 * The sign-off that a migration is finished, which also signs gcloud out of this
 * server: a tenant admin's sign-in must not outlive the migration it was for (live,
 * one had sat in /tmp since August). The next tenant's setup signs in fresh as that
 * tenant's own admin. Shows who is signed in now, so a stale one is seen, not found.
 */
const ApproveComplete: React.FC = () => {
  const [held, setHeld] = useState<GcloudIdentity[] | null>(null)
  const [asking, setAsking] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const load = () => {
    fetchGcloudIdentities().then((r) => setHeld(r.identities)).catch(() => setHeld(null))
  }
  useEffect(() => { load() }, [])
  const accounts = (held ?? []).flatMap((h) => h.accounts)

  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="approve-complete">
      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>Approve this migration as complete</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        Revokes every gcloud sign-in this server holds. The next tenant&apos;s setup signs in
        again as that tenant&apos;s own admin.
      </Typography>
      {held && (
        <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 0.75, mb: 1.5 }} alignItems="center"
               data-testid="gcloud-held">
          <Typography variant="caption" color="text.secondary">gcloud on this server:</Typography>
          {accounts.length
            ? accounts.map((a) => <Chip key={a} size="small" variant="outlined" label={a} />)
            : <Typography variant="caption">nobody is signed in</Typography>}
        </Stack>
      )}
      <Button variant="outlined" size="small" onClick={() => setAsking(true)}
              data-testid="approve-complete-go">
        Approve as complete
      </Button>
      {done && <Alert severity="success" sx={{ mt: 1.5 }} onClose={() => setDone(null)}>{done}</Alert>}
      <ReasonCodeDialog
        open={asking} error={err}
        title="Approve this migration as complete"
        description={<>Revokes every gcloud sign-in on this server
          {accounts.length ? <> ({accounts.join(', ')})</> : null}. Refused while a setup, or a
          job of this account, is still running.</>}
        onCancel={() => { setAsking(false); setErr(null) }}
        onConfirm={async (reason) => {
          try {
            const r = await approveMigrationComplete(reason)
            if (!r.ok) throw new Error(r.detail)
            setDone(r.detail); setAsking(false); setErr(null); load()
          } catch (e) {
            setErr(e instanceof Error ? e.message : String(e))
          }
        }}
      />
    </Paper>
  )
}

export default ApproveComplete
