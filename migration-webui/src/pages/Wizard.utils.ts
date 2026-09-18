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
