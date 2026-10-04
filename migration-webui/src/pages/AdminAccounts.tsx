import React, { useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, CircularProgress, IconButton, Paper, Stack, Switch, Table,
  TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material'
import { AdminPanelSettings as AdminIcon, DeleteOutline as DeleteIcon } from '@mui/icons-material'
import {
  Account, createAccount, deleteAccount, fetchAdminAccounts, setAccountSubscription, setAccountSeedEnabled,
} from '@/api/controlPlane'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'

type Pending = { id: number; email: string } & (
  | { kind: 'subscription'; active: boolean }
  | { kind: 'seed'; enabled: boolean }
  | { kind: 'delete' }
  | { kind: 'create'; name: string; password: string }
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
  const [draft, setDraft] = useState({ email: '', name: '', password: '' })
  const [created, setCreated] = useState<string | null>(null)

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
          : pending.kind === 'create'
            ? await createAccount(pending.email, pending.password, pending.name, reason)
            : await deleteAccount(pending.id, pending.email, reason)
      if (!r.ok) throw new Error(r.detail || 'could not update')
      if (pending.kind === 'create') {
        setCreated(`${r.detail}. Sign in as it to set up its pair in the Setup Wizard.`)
        setDraft({ email: '', name: '', password: '' })
      }
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

      {/* A new pair needs its own account: the Setup Wizard configures whoever is
          signed in, and sign-up is closed once this install has an account. */}
      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>New account</Typography>
        <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 1.5 }} alignItems="center">
          <TextField size="small" label="Sign-in email" value={draft.email}
                     onChange={(e) => setDraft({ ...draft, email: e.target.value })}
                     inputProps={{ 'data-testid': 'new-account-email' }} />
          <TextField size="small" label="Name" value={draft.name}
                     onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                     inputProps={{ 'data-testid': 'new-account-name' }} />
          <TextField size="small" label="Password (8+ characters)" type="password" value={draft.password}
                     onChange={(e) => setDraft({ ...draft, password: e.target.value })}
                     inputProps={{ 'data-testid': 'new-account-password' }} />
          <Button variant="outlined" data-testid="new-account-go"
                  disabled={!draft.email.trim() || draft.name.trim().length < 2 || draft.password.length < 8}
                  onClick={() => setPending({ kind: 'create', id: 0, email: draft.email.trim(),
                                              name: draft.name.trim(), password: draft.password })}>
            Create account
          </Button>
        </Stack>
        {created && <Alert severity="success" sx={{ mt: 1.5 }} onClose={() => setCreated(null)}>{created}</Alert>}
      </Paper>

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
              : pending?.kind === 'delete' ? `Delete ${pending.email}`
                : pending?.kind === 'create' ? `Create account ${pending.email}` : ''
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
          ) : pending?.kind === 'create' ? (
            <>Creates a sign-in with its own empty pair and its own ledger. Nothing is set up
              yet: sign in as it and run the Setup Wizard for its source and target.</>
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
