/**
 * A percentage at the resolution it actually has.
 *
 * The server keeps two decimals now, because rounding to whole numbers
 * reported the first finished user of two hundred as 0% -- work completed,
 * progress none, for the seventy-five minutes it took the second to land.
 *
 * Printing 88.24% next to it would be the opposite mistake: two decimals on
 * a number that moves every few seconds is noise. So the decimals appear
 * only where they carry the information -- below 10%, where whole numbers
 * are too coarse to show anything moving -- and trailing zeros are dropped
 * so a real 0.5 reads as "0.5%" and a real 12 reads as "12%".
 */
export function formatPct(pct: number): string {
  if (!Number.isFinite(pct)) return '--'
  if (pct <= 0) return '0%'
  if (pct >= 10) return `${Math.round(pct)}%`
  // Two decimals, then strip what they add nothing to: 1.50 -> 1.5, 3.00 -> 3.
  return `${Number(pct.toFixed(2))}%`
}
