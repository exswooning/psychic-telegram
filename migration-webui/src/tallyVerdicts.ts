/**
 * Shared verdict copy/colouring for the Tally page. Its own file, not exported from the
 * page component: a page file that exports more than a component breaks React Fast
 * Refresh for it (see oneToOneVerdicts.ts, which exists for the same reason).
 */
import type { TallyVerdict } from '@/api/controlPlane'

export const VERDICT: Record<TallyVerdict, { color: 'success' | 'error' | 'warning' | 'info' | 'default'; label: string; hint: string }> = {
  COMPLETE: { color: 'success', label: 'Complete', hint: 'Every service counted at or above parity with the source, and every Drive item matched its copy.' },
  DIFFERS: { color: 'warning', label: 'Differs', hint: 'The counts agree, but some Drive items are not the same as their copies (name, size, checksum or modified time), or a copy is missing. Open the row for which.' },
  SHORT: { color: 'error', label: 'Short', hint: 'At least one service holds fewer items on the target than expected.' },
  OWED_TO_DMS: { color: 'info', label: 'Owed to DMS', hint: 'Everything is at parity except mail, and every missing message is waiting for Google\'s Data Migration Service. Not a gap in this migration; it clears when the DMS import runs.' },
  UNKNOWN: { color: 'warning', label: 'Unknown', hint: 'A tally ran but nothing could be counted (every service errored). That is not a pass.' },
  NOT_TALLIED: { color: 'default', label: 'Not tallied', hint: 'Nobody has counted this user on both tenants yet.' },
}
export const ORDER: TallyVerdict[] = ['SHORT', 'DIFFERS', 'UNKNOWN', 'OWED_TO_DMS', 'NOT_TALLIED', 'COMPLETE']
