import React, { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { MenuItem, TextField } from '@mui/material'
import { Account, fetchAdminAccounts, fetchMe } from '@/api/controlPlane'
import { labelFor } from '@/utils/accountLabel'
import { VIEWED_KEY, storedViewedAccount } from '@/hooks/useViewedAccount'

/** Superadmin only: pick whose pages to look at. Lives in ?account=, re-added on navigation. */
const ViewingAccountSelect: React.FC = () => {
  const [params, setParams] = useSearchParams()
  const [accounts, setAccounts] = useState<Account[] | null>(null)
  useEffect(() => {
    fetchMe().then((m) => (m.is_superadmin ? fetchAdminAccounts() : null))
      .then(setAccounts).catch(() => setAccounts(null))
  }, [])
  const inUrl = Number(params.get('account')) || undefined
  const kept = storedViewedAccount()
  useEffect(() => {
    if (accounts && !inUrl && kept) {
      const next = new URLSearchParams(params); next.set('account', String(kept))
      setParams(next, { replace: true })
    }
  }, [accounts, inUrl, kept, params, setParams])
  if (!accounts) return null
  const choose = (v: string) => {
    try { if (v) sessionStorage.setItem(VIEWED_KEY, v); else sessionStorage.removeItem(VIEWED_KEY) } catch { /* optional */ }
    const next = new URLSearchParams(params)
    if (v) next.set('account', v); else next.delete('account')
    setParams(next)
  }
  return (
    <TextField select size="small" label="Viewing account" value={inUrl ?? kept ?? ''}
               onChange={(e) => choose(e.target.value)} sx={{ minWidth: 220, mr: 1 }}
               inputProps={{ 'data-testid': 'viewing-account' }}>
      <MenuItem value="">My own account</MenuItem>
      {accounts.map((a) => <MenuItem key={a.id} value={a.id}>{labelFor(a, accounts)}</MenuItem>)}
    </TextField>
  )
}

export default ViewingAccountSelect
