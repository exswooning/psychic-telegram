/**
 * Which migration every page is about, when this login has more than one.
 *
 * Each migration is its own account on the server (keys, ledger, jobs), so switching
 * reloads the app: every page then asks about the chosen one, with nothing cached
 * from the last.
 */
import React, { useEffect, useState } from 'react'
import { MenuItem, TextField } from '@mui/material'
import { fetchMe, fetchMigrations, MigrationRow } from '@/api/controlPlane'
import { chooseMigration } from '@/api/migration'

const label = (m: MigrationRow) =>
  m.sourceDomain || m.targetDomain
    ? `${m.sourceDomain || 'source not chosen'} → ${m.targetDomain || 'destination not chosen'}`
    : m.accountName

export const MigrationSwitcher: React.FC = () => {
  const [rows, setRows] = useState<MigrationRow[]>([])
  const [current, setCurrent] = useState<number | null>(null)
  const [login, setLogin] = useState<number | null>(null)

  useEffect(() => {
    Promise.resolve().then(() => Promise.all([fetchMigrations(), fetchMe()])).then(([m, me]) => {
      setRows(m.migrations)
      setCurrent(me.migration_id ?? me.id)
      setLogin(me.id)
    }).catch(() => {})
  }, [])

  if (rows.length < 2 || current == null) return null
  const known = rows.some((r) => r.accountId === current)
  return (
    <TextField select size="small" value={known ? current : ''} label="Migration"
               data-testid="migration-switcher" sx={{ minWidth: 260, maxWidth: 420 }}
               onChange={(e) => {
                 const id = Number(e.target.value)
                 chooseMigration(id === login ? null : id)
                 window.location.reload()
               }}>
      {rows.map((r) => (
        <MenuItem key={r.accountId} value={r.accountId}>{label(r)}</MenuItem>
      ))}
    </TextField>
  )
}

export default MigrationSwitcher
