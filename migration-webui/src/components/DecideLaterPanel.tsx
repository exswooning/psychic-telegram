/**
 * "Set it up and decide later."
 *
 * Setting a tenant up and choosing what to do with it are two decisions,
 * and the wizard used to force them together: you had to declare seed or
 * migrate before anything would create a Cloud project. But the thing you
 * most want before deciding is a count of what is actually in the tenant --
 * and counting requires the very setup the decision was gating.
 *
 * So this path does the setup, counts, and stops. A migration can be set up
 * afterwards from the same wizard; nothing here forecloses it. What it adds
 * is the two operations you need on a tenant you have not committed to:
 * empty it, or empty it and remove its accounts.
 */
import React, { useState } from 'react'
import { Alert, Box, Button, Divider, Stack, Typography } from '@mui/material'
import { DeleteSweep as WipeIcon, PersonRemove as UsersIcon } from '@mui/icons-material'
import QuickTenantSetup from '@/components/QuickTenantSetup'
import TenantActionDialog from '@/components/TenantActionDialog'
import { removeTenantSetup } from '@/api/client'

export const DecideLaterPanel: React.FC<{
  domain: string
  side?: 'source' | 'target'
  adminEmail?: string
  adminPassword?: string
}> = ({ domain, side = 'source', adminEmail, adminPassword }) => {
  const [act, setAct] = useState<'wipe' | 'delete_users' | null>(null)
  const [note, setNote] = useState('')

  return (
    <Box>
      <Alert severity="info" sx={{ mb: 2 }}>
        {domain} is being set up so it can be read and counted. Nothing here
        commits it to a seed or a migration — choose either later from this
        wizard, and it will use what has already been created.
      </Alert>

      {/* The manual view is the one that carries the inventory panel once
          setup succeeds, which is the entire point of this path. */}
      <QuickTenantSetup side={side} view="manual"
                        initialDomain={domain}
                        initialEmail={adminEmail}
                        initialPassword={adminPassword} />

      <Divider sx={{ my: 3 }} />

      <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 0.5 }}>
        Empty this tenant
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        For a tenant you are evaluating rather than keeping. Both are gated on
        typing the domain back, and neither can remove an administrator —
        every super-admin and delegated admin is kept, because a deleted
        Workspace address stays reserved for 20 days and there would be no
        credential left to undo it with.
      </Typography>
      <Stack direction="row" spacing={1.5} sx={{ flexWrap: 'wrap', gap: 1 }}>
        <Button size="small" color="warning" variant="outlined"
                startIcon={<WipeIcon />} data-testid="later-wipe"
                onClick={() => setAct('wipe')}>
          Wipe data
        </Button>
        <Button size="small" color="error" variant="outlined"
                startIcon={<UsersIcon />} data-testid="later-delete-users"
                onClick={() => setAct('delete_users')}>
          Delete all users
        </Button>
      </Stack>
      {note && (
        <Typography variant="caption" color="success.main"
                    sx={{ display: 'block', mt: 1 }}>
          {note}
        </Typography>
      )}

      <TenantActionDialog
        target={act ? { domain, mode: act } : null}
        onCancel={() => setAct(null)}
        onConfirm={async (password) => {
          const r = await removeTenantSetup(side, domain, password, act!)
          if (!r.ok) throw new Error(r.error || `could not ${act} the tenant`)
          setNote(r.queued
            ? (r.msg || 'queued — it will start on its own')
            : `${act === 'wipe' ? 'wipe' : 'user deletion'} started — follow it on Jobs`)
        }} />
    </Box>
  )
}

export default DecideLaterPanel
