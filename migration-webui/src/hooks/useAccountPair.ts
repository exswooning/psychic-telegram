import { useEffect, useState } from 'react'
import { fetchMigrations } from '@/api/controlPlane'

/**
 * "s.example → t.example" for an account id, or '' while unknown. A confirmation that does not name the
 * tenants it will act on is how the DMS import ran for gorkhatweed while the page showed account 3 (8 Nov
 * 2026): two selectors in the header, and nothing on the button saying which one it used.
 */
export function useAccountPair(accountId?: number | string | null): string {
  const [pair, setPair] = useState('')
  useEffect(() => {
    let live = true
    setPair('')
    if (accountId === undefined || accountId === null || accountId === '') return undefined
    fetchMigrations().then((r) => {
      const m = r.migrations.find((x) => String(x.accountId) === String(accountId))
      if (live && m && m.sourceDomain && m.targetDomain) setPair(`${m.sourceDomain} → ${m.targetDomain}`)
    }).catch(() => { /* no label is better than a wrong one */ })
    return () => { live = false }
  }, [accountId])
  return pair
}
