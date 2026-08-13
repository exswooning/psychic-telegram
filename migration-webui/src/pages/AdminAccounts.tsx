import React, { useEffect, useState } from 'react'
import {
  Alert, Box, Chip, CircularProgress, Paper, Stack, Switch, Table,
  TableBody, TableCell, TableContainer, TableHead, TableRow, Typography,
} from '@mui/material'
import { AdminPanelSettings as AdminIcon } from '@mui/icons-material'
import { Account, fetchAdminAccounts, setAccountSubscription } from '@/api/controlPlane'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'

/**
 * Superadmin only (see require_superadmin in api_server.py) -- everyone
 * else never sees the nav entry that links here, and the backend refuses
 * the underlying calls regardless. The v1 billing gate is a manual toggle,
 * not a Stripe webhook (see accounts_auth.set_subscription_active): this
 * page is that toggle's whole UI.
 */
const AdminAccounts: React.FC = () => {
  const [accounts, setAccounts] = useState<Account[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<{ id: number; active: boolean; email: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const refresh = () => {
    fetchAdminAccounts().then(setAccounts).catch((e) => setError(e.message))
  }

  useEffect(() => { refresh() }, [])

  const confirm = async (reason: string) => {
    if (!pending) return
    setBusy(true); setActionError(null)
    try {
      const r = await setAccountSubscription(pending.id, pending.active, reason)
      if (!r.ok) throw new Error(r.detail || 'could not update subscription')
      setPending(null)
      refresh()
    } catch (e: any) {
      setActionError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 2 }}>
        <AdminIcon color="action" />
        <Typography variant="h5" sx={{ fontWeight: 700 }}>Accounts</Typography>
      </Stack>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      {!accounts && !error && (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
          <CircularProgress size={28} />
        </Box>
      )}

      {accounts && (
        <TableContainer component={Paper} variant="outlined">
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Email</TableCell>
                <TableCell>Name</TableCell>
                <TableCell>Plan</TableCell>
                <TableCell>Signed up</TableCell>
                <TableCell align="center">Superadmin</TableCell>
                <TableCell align="center">Subscription active</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {accounts.map((a) => (
                <TableRow key={a.id} hover>
                  <TableCell>{a.email}</TableCell>
                  <TableCell>{a.name}</TableCell>
                  <TableCell><Chip size="small" label={a.plan} /></TableCell>
                  <TableCell>{new Date(a.created_at).toLocaleDateString()}</TableCell>
                  <TableCell align="center">
                    {a.is_superadmin ? <Chip size="small" color="primary" label="admin" /> : null}
                  </TableCell>
                  <TableCell align="center">
                    <Switch
                      size="small"
                      checked={a.subscription_active}
                      onClick={() => setPending({
                        id: a.id, active: !a.subscription_active, email: a.email,
                      })}
                    />
                  </TableCell>
                </TableRow>
              ))}
              {accounts.length === 0 && (
                <TableRow><TableCell colSpan={6}>No accounts yet.</TableCell></TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      <ReasonCodeDialog
        open={!!pending}
        busy={busy}
        error={actionError}
        title={pending?.active
          ? `Reactivate ${pending.email}`
          : `Deactivate ${pending?.email}`}
        description={pending?.active ? (
          <>Restores access to privileged actions (seeding, migrating, provisioning) for this account.</>
        ) : (
          <>Blocks this account from starting any privileged write action. It can still sign in and view its own data — this is a pause, not a delete.</>
        )}
        onCancel={() => { setPending(null); setActionError(null) }}
        onConfirm={confirm}
      />
    </Box>
  )
}

export default AdminAccounts
