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
  // No domain at all: the wizard has not run, so the login is the only
  // thing left to name it by. A blank row would be unpickable.
  if (!d) return a.email
  const shared = all.filter((o) => domainsOf(o) === d).length > 1
  return shared ? `${d} (${a.email})` : d
}
