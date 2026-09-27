/**
 * Shared between the One-to-one page and its compact summary section on the
 * migration page, so the two can never describe the same verdict differently.
 * Its own file, not exported from a page component: a page file that exports
 * more than a component breaks React Fast Refresh for it.
 */
import type { OneToOneVerdict } from '@/api/controlPlane'

export const VERDICT: Record<OneToOneVerdict, { color: 'success' | 'error' | 'warning' | 'default'; label: string; hint: string }> = {
  IDENTICAL: { color: 'success', label: 'Identical', hint: 'Every item compared matched its original and nothing was left over.' },
  DIFFERENCES: { color: 'error', label: 'Differences', hint: 'Something copied does not match its original, is missing, or was copied twice.' },
  INCOMPLETE: { color: 'warning', label: 'Incomplete', hint: 'Some check could not be made. That is not a pass.' },
  NOT_VERIFIED: { color: 'default', label: 'Not verified', hint: 'Nobody has compared this user against both tenants yet.' },
}
export const ORDER: OneToOneVerdict[] = ['DIFFERENCES', 'INCOMPLETE', 'NOT_VERIFIED', 'IDENTICAL']
