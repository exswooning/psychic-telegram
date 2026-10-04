import React, { useEffect, useState } from 'react'
import { Alert, Box, Button, Chip, Paper, Stack, Typography } from '@mui/material'
import { approveMigrationComplete, fetchLifecycle, LifecycleView, undoApproval } from '@/api/controlPlane'
import { daysUntil } from '@/utils/lifecycle'
import GcloudHeld from './GcloudHeld'
import ReasonCodeDialog from './ReasonCodeDialog'

const day = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : '--')

/**
 * A migration's end of life (lifecycle.py). Approving it -- only this button does;
 * nothing approves a migration on its own -- signs gcloud out of this server and sets
 * the teardown due: each side's Cloud project deleted and delegation revoked (with the
 * admin login kept at setup), then its keys. What will go, and what is left because
 * another account still uses it, is listed before it happens.
 */
const ApproveComplete: React.FC = () => {
  const [refresh, setRefresh] = useState(0)
  const [life, setLife] = useState<LifecycleView | null>(null)
  const [asking, setAsking] = useState<'approve' | 'undo' | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const load = () => {
    fetchLifecycle().then(setLife).catch(() => setLife(null))
    setRefresh((n) => n + 1)
  }
  useEffect(() => { load() }, [])
  const st = life?.state ?? {}
  const due = daysUntil(st.teardown_due_at)

  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="approve-complete">
      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>Approve this migration as complete</Typography>

      {st.torn_down_at ? (
        <Alert severity="info" sx={{ my: 1 }}>Torn down {day(st.torn_down_at)}.</Alert>
      ) : st.approved_at ? (
        <Alert severity="warning" sx={{ my: 1 }} data-testid="teardown-due">
          Approved {day(st.approved_at)} by {st.approved_by}.
          Teardown {due === 0 ? 'is due now' : `in ${due} day${due === 1 ? '' : 's'}`} ({day(st.teardown_due_at)}).
        </Alert>
      ) : (
        <Typography variant="body2" color="text.secondary" sx={{ my: 1 }}>
          Approving signs gcloud out of this server and tears the pair down
          {life ? ` ${life.teardownDays} days later` : ' later'}. Nothing is approved
          until someone presses the button below.
        </Typography>
      )}

      {life && life.plan.length > 0 && !st.torn_down_at && (
        <Box sx={{ mb: 1.5 }} data-testid="teardown-plan">
          <Typography variant="caption" color="text.secondary">The teardown will:</Typography>
          {life.plan.map((s) => (
            <Typography key={s.side} variant="body2" sx={{ ml: 1 }}>
              {s.side} ({s.domain}):{' '}
              {s.leftBecause
                ? <>leave project {s.project || '--'} — {s.leftBecause}</>
                : !s.project && !s.clientId
                  ? <>no key on file</>
                  : <>delete project {s.project}, revoke delegation {s.clientId}{' '}
                      {s.loginKept
                        ? <Chip size="small" color="success" variant="outlined" label={`login kept (${s.adminEmail})`} />
                        : <Chip size="small" color="warning" variant="outlined" label="no login kept — needs GCP Teardown by hand" />}</>}
            </Typography>
          ))}
          <Typography variant="body2" sx={{ ml: 1 }}>then delete this account&apos;s key files and sign gcloud out.</Typography>
        </Box>
      )}

      <GcloudHeld refresh={refresh} />

      {!st.torn_down_at && (
        <Stack direction="row" spacing={1}>
          <Button variant="outlined" size="small" onClick={() => setAsking('approve')}
                  data-testid="approve-complete-go">
            {st.approved_at ? 'Approve again (sign gcloud out)' : 'Approve as complete'}
          </Button>
          {st.approved_at && (
            <Button size="small" onClick={() => setAsking('undo')} data-testid="undo-approval">
              Undo approval
            </Button>
          )}
        </Stack>
      )}
      {done && <Alert severity="success" sx={{ mt: 1.5 }} onClose={() => setDone(null)}>{done}</Alert>}

      <ReasonCodeDialog
        open={!!asking} error={err}
        title={asking === 'undo' ? 'Take the approval back' : 'Approve this migration as complete'}
        description={asking === 'undo'
          ? <>No teardown will be due until it is approved again — for a re-run.</>
          : <>Revokes every gcloud sign-in on this server (listed above) and sets the
              teardown above due in {life ? life.teardownDays : '--'} days.
              Refused while a setup, or a job of this account, is still running.</>}
        destructive={asking === 'approve'}
        onCancel={() => { setAsking(null); setErr(null) }}
        onConfirm={async (reason) => {
          try {
            const r = asking === 'undo' ? await undoApproval(reason) : await approveMigrationComplete(reason)
            if (!r.ok) throw new Error(r.detail)
            setDone(r.detail); setAsking(null); setErr(null); load()
          } catch (e) {
            setErr(e instanceof Error ? e.message : String(e))
          }
        }}
      />
    </Paper>
  )
}

export default ApproveComplete
