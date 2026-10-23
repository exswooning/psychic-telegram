/**
 * Pause or resume an account's automatic repairs (verification_repairs): when on, a
 * user with open findings is re-checked and repaired on the target while the account
 * is idle. Paused, nothing is repaired on its own -- e.g. while a first-pass benchmark
 * must measure the run alone.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { FormControlLabel, Switch, Tooltip } from '@mui/material'
import ReasonCodeDialog from './ReasonCodeDialog'
import { fetchRepairPolicy, setRepairPolicy } from '@/api/controlPlane'

const RepairPolicySwitch: React.FC<{ accountId?: number }> = ({ accountId }) => {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [asking, setAsking] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => {
    if (accountId) fetchRepairPolicy(accountId).then((r) => setEnabled(r.enabled)).catch(() => setEnabled(null))
  }, [accountId])
  useEffect(() => { load() }, [load])
  if (enabled === null) return null
  return (
    <>
      <Tooltip title="When on, a user with open findings is checked again and repaired on the target while this account is idle.">
        <FormControlLabel
          control={<Switch size="small" checked={enabled} onChange={(e) => setAsking(e.target.checked)}
                           inputProps={{ 'aria-label': 'automatic repairs' }} />}
          label={enabled ? 'Automatic repairs on' : 'Automatic repairs paused'} data-testid="repair-policy" />
      </Tooltip>
      <ReasonCodeDialog
        open={asking !== null} busy={busy}
        title={asking ? 'Resume automatic repairs' : 'Pause automatic repairs'}
        description={asking
          ? 'Users with open findings are checked again and repaired on the target while this account is idle.'
          : 'Nothing is repaired on its own until resumed. A repair already running finishes its user.'}
        onCancel={() => setAsking(null)}
        onConfirm={async (reason) => {
          if (asking === null) return
          setBusy(true)
          try { await setRepairPolicy(asking, reason, accountId); setAsking(null); load() } finally { setBusy(false) }
        }} />
    </>
  )
}

export default RepairPolicySwitch
