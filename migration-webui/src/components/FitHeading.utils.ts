/** The size that makes `measured` fit inside `available`, in rem.
 *
 *  Pure so it can be tested: jsdom reports every layout dimension as 0, so
 *  the DOM half of this component is untestable there and the arithmetic is
 *  the part worth checking anyway.
 */
export function fitFontRem(available: number, measured: number,
                           maxRem: number, minRem: number): number {
  // Nothing measurable yet (jsdom, display:none, first paint): stay at the
  // design size rather than collapsing to the minimum, which would flash
  // small and then grow.
  if (!available || !measured) return maxRem
  if (measured <= available) return maxRem
  return Math.max(minRem, (maxRem * available) / measured)
}
