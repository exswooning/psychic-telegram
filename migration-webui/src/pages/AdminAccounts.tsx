import React, { useEffect, useState } from 'react'
import {
  Alert, Box, Chip, CircularProgress, IconButton, Paper, Stack, Switch, Table,
  TableBody, TableCell, TableContainer, TableHead, TableRow, Typography,
} from '@mui/material'
import { AdminPanelSettings as AdminIcon, DeleteOutline as DeleteIcon } from '@mui/icons-material'
import {
  Account, deleteAccount, fetchAdminAccounts, setAccountSubscription, setAccountSeedEnabled,
} from '@/api/controlPlane'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'

type Pending = { id: number; email: string } & (
  | { kind: 'subscription'; active: boolean }
  | { kind: 'seed'; enabled: boolean }
  | { kind: 'delete' }
)

/**
 * Superadmin only (see require_superadmin in api_server.py) -- everyone
 * else never sees the nav entry that links here, and the backend refuses
 * the underlying calls regardless. The v1 billing gate is a manual toggle,
 * not a Stripe webhook (see accounts_auth.set_subscription_active): this
 * page is that toggle's whole UI. Seed enabled is the same idea, opposite
 * default (opt-in, not opt-out) -- see accounts_auth.set_seed_enabled.
 */
const AdminAccounts: React.FC = () => {
  const [accounts, setAccounts] = useState<Account[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)
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
      const r = pending.kind === 'subscription'
        ? await setAccountSubscription(pending.id, pending.active, reason)
        : pending.kind === 'seed'
          ? await setAccountSeedEnabled(pending.id, pending.enabled, reason)
          : await deleteAccount(pending.id, pending.email, reason)
      if (!r.ok) throw new Error(r.detail || 'could not update')
      setPending(null)
      refresh()
    } catch (e: unknown) {
      setActionError((e instanceof Error ? e.message : String(e)))
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
                <TableCell align="center">Seed enabled</TableCell>
                <TableCell />
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
                        id: a.id, email: a.email,
                        kind: 'subscription', active: !a.subscription_active,
                      })}
                    />
                  </TableCell>
                  <TableCell align="center">
                    <Switch
                      size="small"
                      checked={a.seed_enabled}
                      onClick={() => setPending({
                        id: a.id, email: a.email,
                        kind: 'seed', enabled: !a.seed_enabled,
                      })}
                    />
                  </TableCell>
                  <TableCell align="center">
                    {!a.is_superadmin && (
                      <IconButton size="small" color="error" aria-label={`delete ${a.email}`}
                                  data-testid={`delete-account-${a.id}`}
                                  onClick={() => setPending({ id: a.id, email: a.email, kind: 'delete' })}>
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    )}
                  </TableCell>
                </TableRow>
              ))}
              {accounts.length === 0 && (
                <TableRow><TableCell colSpan={8}>No accounts yet.</TableCell></TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      <ReasonCodeDialog
        open={!!pending}
        busy={busy}
        error={actionError}
        title={
          pending?.kind === 'subscription'
            ? (pending.active ? `Reactivate ${pending.email}` : `Deactivate ${pending.email}`)
            : pending?.kind === 'seed'
              ? (pending.enabled ? `Enable seeding for ${pending.email}` : `Disable seeding for ${pending.email}`)
              : pending?.kind === 'delete' ? `Delete ${pending.email}` : ''
        }
        description={
          pending?.kind === 'subscription' ? (
            pending.active
              ? <>Restores access to privileged actions (seeding, migrating, provisioning) for this account.</>
              : <>Blocks this account from starting any privileged write action. It can still sign in and view its own data — this is a pause, not a delete.</>
          ) : pending?.kind === 'seed' ? (
            pending.enabled
              ? <>Lets this account write fabricated test data into its own source tenant, from the Setup Wizard's Seed option.</>
              : <>Removes this account's ability to seed a tenant with fabricated data. Does not affect a real migration.</>
          ) : pending?.kind === 'delete' ? (
            <>Deletes the account and signs it out everywhere. Refused if it has a tenant set up
              or a job running — for throwaway and test accounts. Type its email to confirm.</>
          ) : null
        }
        confirmPhrase={pending?.kind === 'delete' ? pending.email : undefined}
        destructive={pending?.kind === 'delete'}
        onCancel={() => { setPending(null); setActionError(null) }}
        onConfirm={confirm}
      />
    </Box>
  )
}

export default AdminAccounts
