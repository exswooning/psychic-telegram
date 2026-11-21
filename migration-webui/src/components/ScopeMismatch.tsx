/**
 * Two pickers sit side by side in the workspace header and answer different questions:
 * "Migration" decides what Overview, People and Activity load; "Viewing account" is a
 * superadmin filter that only some pages follow. When they disagree the page quietly shows
 * one account's numbers under the other's name (live, 2026-11-01: Overview showed
 * gorkhatweed.com's 2 people and 155 files while "Viewing account" read s. -> t.).
 *
 * So the header says which one the page is really about, and offers the switch when this
 * login can make it.
 */
import React, { useEffect, useState } from 'react'
import { Alert, Button } from '@mui/material'
import { fetchMe, fetchMigrations, MigrationRow } from '@/api/controlPlane'
import { chooseMigration } from '@/api/migration'
import { useViewedAccount } from '@/hooks/useViewedAccount'

const pair = (m: MigrationRow) =>
  m.sourceDomain || m.targetDomain
    ? `${m.sourceDomain || 'source not chosen'} → ${m.targetDomain || 'destination not chosen'}`
    : m.accountName

export const ScopeMismatch: React.FC = () => {
  const viewed = useViewedAccount()
  const [rows, setRows] = useState<MigrationRow[]>([])
  const [current, setCurrent] = useState<number | null>(null)
  const [login, setLogin] = useState<number | null>(null)

  useEffect(() => {
    Promise.resolve().then(() => Promise.all([fetchMigrations(), fetchMe()])).then(([m, me]) => {
      setRows(m.migrations); setCurrent(me.migration_id ?? me.id); setLogin(me.id)
    }).catch(() => {})
  }, [])

  if (!viewed || current == null || viewed === current) return null
  const shown = rows.find((r) => r.accountId === current)
  const wanted = rows.find((r) => r.accountId === viewed)
  return (
    <Alert severity="warning" sx={{ mb: 2 }} data-testid="scope-mismatch"
           action={wanted ? (
             <Button color="inherit" size="small" onClick={() => {
               chooseMigration(viewed === login ? null : viewed)
               window.location.reload()
             }}>Show {pair(wanted)}</Button>
           ) : undefined}>
      Overview, People and Activity are showing {shown ? pair(shown) : 'another migration'}, but
      “Viewing account” is set to {wanted ? pair(wanted) : `account ${viewed}`}.
    </Alert>
  )
}

export default ScopeMismatch
