/**
 * The illustration beside each setup step.
 *
 * It began as a labelled schematic -- tenant boxes, arrows, captions -- which
 * read as a figure lifted out of documentation on the first screen of a
 * setup. It is composed rather than annotated now: layered cards on a
 * gradient field, depth from shadow and a lit top edge, a sweep of colour
 * carrying the eye across. The meaning survives in the arrangement (two
 * cards and a one-way sweep for a migration, one card being filled for a
 * seed) with no text inside the artwork at all. The words live under it.
 *
 * The composition fills its frame deliberately. The first version drew
 * inside about 70% of the viewBox width and half its height, so the whole
 * thing rendered small with a dead band above the headline -- the empty
 * space read as a mistake rather than as air.
 *
 * Hand-authored SVG: native shapes, gradients, one blur, sized by viewBox
 * and scaled by CSS. No dependency, crisp at any width. Every colour comes
 * from the theme or is chosen per mode -- a fill hardcoded for one ground
 * is the classic unreadable-illustration bug.
 */
import React from 'react'
import { useTheme } from '@mui/material/styles'
import { Box } from '@mui/material'

export type ArtKind = 'setup' | 'seed' | 'migrate'

const ART_LABEL: Record<ArtKind, string> = {
  setup: 'A tenant, with the project and credentials created alongside it',
  seed: 'Fabricated content falling into a single tenant',
  migrate: 'Content sweeping from one tenant into another, in one direction only',
}

export const WizardArt: React.FC<{ kind: ArtKind }> = ({ kind }) => {
  const t = useTheme()
  const dark = t.palette.mode === 'dark'
  const c = {
    // The card faces sit ON the aside panel, which is `paper` in dark mode,
    // so `paper` here would make them invisible against it. One step
    // further from the page in each direction.
    card: dark ? '#35363a' : '#ffffff',
    edge: dark ? 'rgba(255,255,255,0.09)' : 'rgba(0,0,0,0.07)',
    // A lit top edge is what stops a flat rectangle reading as a hole in
    // dark mode. Real surfaces catch light on the side facing it.
    lit: dark ? 'rgba(255,255,255,0.16)' : 'rgba(255,255,255,0.9)',
    accent: t.palette.primary.main,
    soft: t.palette.primary.light,
    // A second hue, kept close enough to stay one family. The teal side of
    // Google's own range rather than a contrasting colour -- the point is
    // depth in the field, not a second signal competing with the accent.
    accent2: dark ? '#78d9ec' : '#12b5cb',
    dots: dark ? 0.10 : 0.07,
    glow: dark ? 0.42 : 0.26,
    shadow: dark ? 0.55 : 0.16,
    bar: dark ? 0.30 : 0.16,
  }
  const uid = `wa-${kind}`

  /** A card face. No text: the composition carries it. */
  const Card = ({ x, y, w, h, o = 1, lines = 3, lead = true }:
                { x: number; y: number; w: number; h: number
                  o?: number; lines?: number; lead?: boolean }) => (
    <g opacity={o}>
      <rect x={x} y={y} width={w} height={h} rx={18} fill={c.card}
            stroke={c.edge} strokeWidth={1} filter={`url(#${uid}-sh)`} />
      {/* The lit edge: an arc across the top corners only. */}
      <path d={`M ${x + 18} ${y + 0.5} H ${x + w - 18}`}
            stroke={c.lit} strokeWidth={1} fill="none" />
      {lead && (
        <>
          <rect x={x + 22} y={y + 24} width={Math.min(w * 0.4, 74)} height={9}
                rx={4.5} fill={c.accent} />
          <rect x={x + 22} y={y + 41} width={w - 44} height={1}
                fill={c.edge} />
        </>
      )}
      {Array.from({ length: lines }).map((_, i) => (
        <rect key={i} x={x + 22} y={y + (lead ? 56 : 26) + i * 18} rx={4}
              // Widths that ebb rather than march: uniform bars read as a
              // placeholder, which is what they are trying not to look like.
              width={(w - 44) * [0.96, 0.72, 0.88, 0.54][i % 4]}
              height={8} fill={c.accent} opacity={c.bar} />
      ))}
    </g>
  )

  /** A row card: a dot and two bars, for the satellites. */
  const Row = ({ x, y, w, h }: { x: number; y: number; w: number; h: number }) => (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={16} fill={c.card}
            stroke={c.edge} strokeWidth={1} filter={`url(#${uid}-sh)`} />
      <path d={`M ${x + 16} ${y + 0.5} H ${x + w - 16}`}
            stroke={c.lit} strokeWidth={1} fill="none" />
      <circle cx={x + 36} cy={y + h / 2} r={17} fill={c.soft}
              opacity={dark ? 0.22 : 0.5} />
      <circle cx={x + 36} cy={y + h / 2} r={17} fill="none"
              stroke={c.accent} strokeWidth={1.5} opacity={0.45} />
      <circle cx={x + 36} cy={y + h / 2} r={6} fill={c.accent} opacity={0.85} />
      <rect x={x + 68} y={y + h / 2 - 14} width={w - 100} height={9} rx={4.5}
            fill={c.accent} opacity={0.5} />
      <rect x={x + 68} y={y + h / 2 + 3} width={(w - 100) * 0.62} height={8} rx={4}
            fill={c.accent} opacity={c.bar} />
    </g>
  )

  return (
    <Box sx={{ mb: { xs: 3, md: 4 }, mx: { xs: -1, md: -1.5 } }}>
      <svg viewBox="0 0 560 300" role="img" width="100%"
           style={{ height: 'auto', display: 'block' }}
           aria-label={ART_LABEL[kind]}>
        <defs>
          <linearGradient id={`${uid}-g`} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor={c.accent} stopOpacity="0.9" />
            <stop offset="100%" stopColor={c.soft} stopOpacity="0.12" />
          </linearGradient>
          <linearGradient id={`${uid}-sweep`} x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor={c.accent} stopOpacity="0" />
            <stop offset="40%" stopColor={c.accent} stopOpacity="0.55" />
            <stop offset="100%" stopColor={c.accent} stopOpacity="0.95" />
          </linearGradient>
          <linearGradient id={`${uid}-g2`} x1="1" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={c.accent2} stopOpacity="0.7" />
            <stop offset="100%" stopColor={c.accent2} stopOpacity="0" />
          </linearGradient>
          {/* A fine dot grid. Barely visible on its own -- what it does is
              give the empty field a surface, so the cards read as sitting
              ON something rather than floating in a void. */}
          <pattern id={`${uid}-dots`} width="18" height="18"
                   patternUnits="userSpaceOnUse">
            <circle cx="1.5" cy="1.5" r="1.5" fill={c.accent} opacity={c.dots} />
          </pattern>
          <radialGradient id={`${uid}-fade`} cx="0.5" cy="0.5" r="0.5">
            <stop offset="0%" stopColor="#fff" stopOpacity="1" />
            <stop offset="100%" stopColor="#fff" stopOpacity="0" />
          </radialGradient>
          <mask id={`${uid}-mask`}>
            <rect width="560" height="300" fill={`url(#${uid}-fade)`} />
          </mask>
          <filter id={`${uid}-blur`} x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="42" />
          </filter>
          <filter id={`${uid}-sh`} x="-40%" y="-40%" width="180%" height="200%">
            <feDropShadow dx="0" dy="10" stdDeviation="14"
                          floodColor="#000" floodOpacity={c.shadow} />
          </filter>
        </defs>

        {/* One soft field of colour, placed where the eye should land first
            for this step. */}
        <ellipse cx={kind === 'setup' ? 190 : 280} cy={150} rx={215} ry={125}
                 fill={`url(#${uid}-g)`} opacity={c.glow}
                 filter={`url(#${uid}-blur)`} />
        {/* A second field, offset and in a different hue. One blob reads as
            a spotlight; two overlapping ones read as depth. */}
        <ellipse cx={kind === 'setup' ? 430 : 400} cy={210} rx={165} ry={110}
                 fill={`url(#${uid}-g2)`} opacity={c.glow * 0.75}
                 filter={`url(#${uid}-blur)`} />
        <rect width="560" height="300" fill={`url(#${uid}-dots)`}
              mask={`url(#${uid}-mask)`} />

        {kind === 'setup' && (
          <>
            <Card x={26} y={38} w={244} h={224} lines={4} />
            <Row x={318} y={58} w={216} h={86} />
            <Row x={318} y={168} w={216} h={86} />
            <path d="M 282 128 C 300 118, 302 106, 312 101" fill="none"
                  stroke={c.accent} strokeWidth={2.5} opacity={0.45}
                  strokeLinecap="round" />
            <path d="M 282 168 C 300 178, 302 200, 312 207" fill="none"
                  stroke={c.accent} strokeWidth={2.5} opacity={0.45}
                  strokeLinecap="round" />
          </>
        )}

        {kind === 'migrate' && (
          <>
            {/* One direction, stated by the gradient itself: it fades in at
                the source and arrives solid at the destination. */}
            <path d="M 130 246 C 250 268, 330 250, 452 190" fill="none"
                  stroke={`url(#${uid}-sweep)`} strokeWidth={4}
                  strokeLinecap="round" />
            <Card x={40} y={58} w={186} h={150} o={0.5} lines={2} />
            <Card x={22} y={40} w={200} h={166} lines={3} />
            <Card x={338} y={92} w={186} h={150} o={0.5} lines={2} />
            <Card x={356} y={74} w={200} h={166} lines={3} />
            {[0, 1, 2].map((i) => (
              <circle key={i} cx={252 + i * 44} cy={244 - i * 16}
                      r={6 - i * 0.8} fill={c.accent} opacity={0.8 - i * 0.2} />
            ))}
          </>
        )}

        {kind === 'seed' && (
          <>
            {[0, 1, 2, 3].map((i) => (
              <g key={i}>
                <rect x={176 + i * 56} y={16 + (i % 2) * 18}
                      width={34} height={34} rx={11}
                      fill={c.accent} opacity={0.2 + i * 0.14} />
                <line x1={193 + i * 56} y1={58 + (i % 2) * 18}
                      x2={193 + i * 56} y2={104} stroke={c.accent}
                      strokeWidth={2.5} strokeLinecap="round"
                      opacity={0.32} strokeDasharray="2 9" />
              </g>
            ))}
            <Card x={158} y={118} w={244} h={164} lines={4} />
          </>
        )}
      </svg>
    </Box>
  )
}

export default WizardArt
