import { fetchMe, fetchMigrations, openMigration } from '@/api/controlPlane'
import { chooseMigration } from '@/api/migration'

/** Enough to catch a typo, not enough to argue with a real domain.
 *  Deliberately not a strict RFC pattern: this gates a form, and every
 *  over-tight domain regex eventually rejects somebody's valid TLD. */
export function looksLikeDomain(v: string): boolean {
  const d = v.trim().toLowerCase()
  return /^[a-z0-9.-]+\.[a-z]{2,}$/.test(d) && !d.startsWith('.') && !d.endsWith('.')
}

/** The domain is in the email. Asking for both is asking someone to type
 *  the same fact twice and then handling the case where they disagree. */
export function domainOf(email: string): string {
  const at = email.trim().toLowerCase().lastIndexOf('@')
  return at === -1 ? '' : email.trim().toLowerCase().slice(at + 1)
}

/** Point this browser at the migration this pair belongs in. Each migration is its
 *  own account (keys, ledger, jobs): the same pair goes back to its own, and a pair
 *  that differs from the one in hand gets a new one -- live, setting up dharan.gov.np
 *  on the account holding gorkhatweed.com moved gorkhatweed's keys aside. */
export async function placeMigration(src: string, tgt: string): Promise<void> {
  const [{ migrations }, me] = await Promise.all([fetchMigrations(), fetchMe()])
  const here = me.migration_id ?? me.id
  const same = src && tgt
    ? migrations.find((m) => m.sourceDomain === src && m.targetDomain === tgt) : undefined
  if (same) {
    if (same.accountId !== here) chooseMigration(same.accountId === me.id ? null : same.accountId)
    return
  }
  const cur = migrations.find((m) => m.accountId === here)
  const clash = !!cur && ((!!cur.sourceDomain && !!src && cur.sourceDomain !== src)
                          || (!!cur.targetDomain && !!tgt && cur.targetDomain !== tgt))
  if (!clash) return
  // An empty one this login already opened: a setup that failed leaves its migration
  // without domains, and trying again must not open another beside it.
  const empty = migrations.find((m) => m.ownerId === me.id && !m.sourceDomain && !m.targetDomain)
  if (empty) {
    chooseMigration(empty.accountId)
    return
  }
  const made = await openMigration(`${src || 'source to choose'} → ${tgt || 'destination to choose'}`)
  if (!made.ok || made.accountId == null) throw new Error(made.detail || 'could not open a new migration')
  chooseMigration(made.accountId)
}
