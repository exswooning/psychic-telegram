/**
 * A heading that shrinks to fit its column on one line.
 *
 * The wizard puts the tenant's own domain in the display heading, and a
 * fixed 3.25rem broke `source.rohitrokaya.com.np` across two lines mid-word
 * -- "source.rohitrokaya.co" / "m.np" -- which reads as a rendering fault
 * rather than a long name. A domain is a single token; there is no correct
 * place to break one.
 *
 * Measured rather than guessed from character count. Character width varies
 * enough between "iiiii" and "WWWWW" that a count-based estimate is either
 * too small for most names or still overflows for some, and the column
 * width itself changes with the viewport. scrollWidth against the parent's
 * clientWidth is the actual answer to the actual question.
 *
 * The size is written to the node's own style rather than held in state:
 * re-rendering on every measurement, while a ResizeObserver watches, is how
 * this becomes an infinite loop. The observer watches the PARENT, whose
 * width does not depend on the child's font size, for the same reason.
 */
import React, { useLayoutEffect, useRef } from 'react'
import { Box } from '@mui/material'
import type { SxProps, Theme } from '@mui/material'

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

export const FitHeading: React.FC<{
  text: string
  maxRem?: number
  minRem?: number
  sx?: SxProps<Theme>
}> = ({ text, maxRem = 3.25, minRem = 1.375, sx }) => {
  const ref = useRef<HTMLDivElement | null>(null)

  useLayoutEffect(() => {
    const el = ref.current
    const box = el?.parentElement
    if (!el || !box) return
    const fit = () => {
      // Reset first: measuring at the current (possibly shrunken) size
      // would ratchet downward and never recover when the text gets
      // shorter or the window wider.
      el.style.fontSize = `${maxRem}rem`
      el.style.fontSize =
        `${fitFontRem(box.clientWidth, el.scrollWidth, maxRem, minRem)}rem`
    }
    fit()
    // Feature-detected, not assumed. Without ResizeObserver the heading
    // still fits itself once, on mount -- which is the case that matters --
    // instead of the whole page dying on a ReferenceError. jsdom is the
    // environment that proved this, but an old browser behaves the same.
    if (typeof ResizeObserver === 'undefined') return undefined
    const ro = new ResizeObserver(fit)
    ro.observe(box)
    return () => ro.disconnect()
  }, [text, maxRem, minRem])

  return (
    <Box
      ref={ref}
      component="h1"
      sx={{
        // nowrap is what makes the measurement meaningful: allowed to wrap,
        // scrollWidth reports the wrapped width and always "fits".
        whiteSpace: 'nowrap',
        fontSize: `${maxRem}rem`,
        fontWeight: 400, lineHeight: 1.15, letterSpacing: '-0.5px',
        m: 0,
        ...sx,
      }}>
      {text}
    </Box>
  )
}

export default FitHeading
