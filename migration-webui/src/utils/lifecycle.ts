import type { LifecycleView } from '@/api/controlPlane'

/** Whole days until `iso`, never negative; null without a date. */
export const daysUntil = (iso?: string | null, now = Date.now()): number | null =>
  iso ? Math.max(0, Math.ceil((new Date(iso).getTime() - now) / 86_400_000)) : null

/** One pair's end of life in a word or three -- the same reading on every page.
 *  Only an operator's click approves (lifecycle.py), so "not approved" is the
 *  resting state of every pair, not a warning. */
export const lifecycleLabel = (state: LifecycleView['state'] | undefined, now = Date.now()):
  { label: string; tone: 'default' | 'warning' | 'info'; title?: string } => {
  if (state?.torn_down_at) {
    return { label: 'torn down', tone: 'info', title: new Date(state.torn_down_at).toLocaleString() }
  }
  if (state?.approved_at) {
    const d = daysUntil(state.teardown_due_at, now)
    return { label: d === 0 ? 'teardown due now' : `teardown in ${d} day${d === 1 ? '' : 's'}`,
             tone: 'warning', title: `approved by ${state.approved_by}` }
  }
  return { label: 'not approved', tone: 'default' }
}
