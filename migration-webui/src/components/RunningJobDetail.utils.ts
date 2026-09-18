/** Straight-line projection from what has elapsed and what is done.
 *  Only honest while the rate holds, which is why it is labelled as a
 *  projection and rendered "--" rather than 0 when there is nothing to
 *  project from. */
export function projectedEta(pct: number | null | undefined,
                             elapsedSec: number | undefined): number | null {
  if (typeof pct !== 'number' || !elapsedSec || pct <= 0 || pct >= 100) return null
  return (elapsedSec / pct) * (100 - pct)
}
