/**
 * Shared verdict copy/colouring for the Tally page. Its own file, not exported from the
 * page component: a page file that exports more than a component breaks React Fast
 * Refresh for it (see oneToOneVerdicts.ts, which exists for the same reason).
 */
import type { TallyVerdict } from '@/api/controlPlane'

export const VERDICT: Record<TallyVerdict, { color: 'success' | 'error' | 'warning' | 'default'; label: string; hint: string }> = {
  COMPLETE: { color: 'success', label: 'Complete', hint: 'Every service counted at or above parity with the source.' },
  SHORT: { color: 'error', label: 'Short', hint: 'At least one service holds fewer items on the target than expected.' },
  UNKNOWN: { color: 'warning', label: 'Unknown', hint: 'A tally ran but nothing could be counted (every service errored). That is not a pass.' },
  NOT_TALLIED: { color: 'default', label: 'Not tallied', hint: 'Nobody has counted this user on both tenants yet.' },
}
export const ORDER: TallyVerdict[] = ['SHORT', 'UNKNOWN', 'NOT_TALLIED', 'COMPLETE']
