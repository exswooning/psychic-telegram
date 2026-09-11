/**
 * Pick a tenant by its domains.
 *
 * The third place this was a free-text "Account id" box. Nobody recognises
 * 7, which is how it was reported the first two times -- and here it sits
 * next to "Wipe target accounts", where the cost of a mistyped digit is
 * somebody else's tenant rather than a failed job.
 *
 * Keeps a STRING value and "" for "my own account", because that is the
 * contract every call site already had: the API treats an absent account as
 * the caller's own, and turning that into a sentinel number here would put
 * the same ambiguity back one layer down.
 */
import React, { useEffect, useState } from 'react'
import { MenuItem, TextField } from '@mui/material'
import { fetchAdminAccounts, fetchMe } from '@/api/controlPlane'
import type { Account } from '@/api/controlPlane'
import { labelFor } from '@/utils/accountLabel'

export const TenantSelect: React.FC<{
  value: string
  onChange: (value: string) => void
  /** Shown for "" -- the caller's own account. */
  mineLabel?: string
  label?: string
  width?: number
  testid?: string
}> = ({ value, onChange, mineLabel = 'My own tenant',
        label = 'Tenant', width = 300, testid }) => {
  const [accounts, setAccounts] = useState<Account[]>([])

  useEffect(() => {
    fetchMe()
      .then((me) => {
        if (!(me as Account).is_superadmin) return
        fetchAdminAccounts().then(setAccounts).catch(() => { /* "" still works */ })
      })
      .catch(() => { /* no list: the blank option is the whole chooser */ })
  }, [])

  return (
    <TextField select size="small" label={label} sx={{ minWidth: width }}
               value={value} onChange={(e) => onChange(e.target.value)}
               inputProps={testid ? { 'data-testid': testid } : undefined}>
      <MenuItem value="">{mineLabel}</MenuItem>
      {accounts.map((a) => (
        <MenuItem key={a.id} value={String(a.id)}>{labelFor(a, accounts)}</MenuItem>
      ))}
    </TextField>
  )
}

export default TenantSelect
