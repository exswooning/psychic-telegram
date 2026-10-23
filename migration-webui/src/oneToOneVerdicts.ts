/**
 * Shared between the One-to-one page and its compact summary section on the
 * migration page, so the two can never describe the same verdict differently.
 * Its own file, not exported from a page component: a page file that exports
 * more than a component breaks React Fast Refresh for it.
 */
import type { OneToOneService, OneToOneVerdict } from '@/api/controlPlane'

export const VERDICT: Record<OneToOneVerdict, { color: 'success' | 'error' | 'warning' | 'default'; label: string; hint: string }> = {
  IDENTICAL: { color: 'success', label: 'Identical', hint: 'Every item compared matched its original and nothing was left over.' },
  DIFFERENCES: { color: 'error', label: 'Differences', hint: 'Something copied does not match its original, is missing, or was copied twice.' },
  INCOMPLETE: { color: 'warning', label: 'Incomplete', hint: 'Some check could not be made. That is not a pass.' },
  NOT_VERIFIED: { color: 'default', label: 'Not verified', hint: 'Nobody has compared this user against both tenants yet.' },
}
export const ORDER: OneToOneVerdict[] = ['DIFFERENCES', 'INCOMPLETE', 'NOT_VERIFIED', 'IDENTICAL']

const WRONG: Array<[keyof NonNullable<OneToOneService['counts']>, string]> = [
  ['differences', 'not identical'], ['missing', 'missing on the target'], ['notCopied', 'not copied'],
  ['duplicates', 'copied twice'], ['extras', 'on the target only'], ['errors', 'could not be compared'],
]

/** "drive: 15 checked · 14 identical · 1 not identical". Not "14/15", which read as one
 *  file missing when all 15 were there and one differed by a line break. */
export const serviceSummary = (s: OneToOneService): string => {
  const completion = s.completion === 'INCOMPLETE' ? ['check incomplete'] : []
  if (!s.checked) return [`${s.service}: nothing to compare`, ...completion].join(' · ')
  const of = s.sampledOf && s.sampledOf > s.checked ? ` of ${s.sampledOf.toLocaleString()}` : ''
  const wrong = WRONG.filter(([k]) => (s.counts?.[k] ?? 0) > 0)
    .map(([k, what]) => `${(s.counts?.[k] ?? 0).toLocaleString()} ${what}`)
  const same = s.identical === s.checked && !wrong.length ? 'all identical' : `${s.identical.toLocaleString()} identical`
  return [`${s.service}: ${s.checked.toLocaleString()} checked${of}`, same, ...wrong, ...completion].join(' · ')
}
