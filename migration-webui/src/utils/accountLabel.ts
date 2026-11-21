/**
 * What to call a tenant in a chooser.
 *
 * Bitport addresses tenants by account id, because that is what the API
 * targets. Nobody recognises one. Put a raw id on screen and you get the
 * question it earns -- "what is account id?" -- which has now been asked
 * about two different controls.
 *
 * The domain pair is the name an operator knows. The login comes back only
 * as a tiebreak, and only on the rows that need one: live, three accounts
 * point at source.rohitrokaya.com.np -> target.rohitrokaya.com.np, and
 * three identical lines is the same problem as three numbers.
 */
export interface LabelledAccount {
  id: number
  email: string
  /** The wizard names a migration account after its pair before any tenant is set up. */
  name?: string | null
  source_domain?: string | null
  target_domain?: string | null
}

/** The source-to-target pair, or whichever single side is configured. */
export const domainsOf = (a: LabelledAccount): string =>
  a.source_domain && a.target_domain
    ? `${a.source_domain} → ${a.target_domain}`
    : (a.source_domain || a.target_domain || '')

export const labelFor = (a: LabelledAccount, all: LabelledAccount[]): string => {
  const d = domainsOf(a)
  // No domain at all: setup has not run. The account's own name is what the operator typed or the
  // wizard made from the pair ("a.x.com → b.x.com"); the machine-made login ("migration-2-4c22631c@...")
  // read as a stranger's account and could not be picked out (17 Nov 2026). Only when there is no
  // name is the login left -- a blank row would be unpickable.
  if (!d) return a.name && a.name !== a.email ? `${a.name} (setup not run)` : a.email
  const shared = all.filter((o) => domainsOf(o) === d).length > 1
  return shared ? `${d} (${a.email})` : d
}
