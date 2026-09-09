/**
 * The illustration beside each setup step.
 *
 * This was a labelled schematic -- tenant boxes, arrows, captions. Correct,
 * and it read as a figure lifted out of documentation: the panel Google
 * fills with an atmospheric product render was answering a question nobody
 * had asked yet. A setup's first screen is not where someone studies a
 * diagram.
 *
 * So it is composed rather than annotated: layered cards on a soft gradient
 * field, depth from shadow, a sweep of colour carrying the eye from one
 * side to the other. The meaning survives in the composition -- two cards
 * and a one-way sweep for a migration, one card being filled for a seed --
 * without a single label inside the artwork. The words live under it, the
 * way they do on the page this is modelled on.
 *
 * Hand-authored SVG: native shapes, gradients and one blur, sized by
 * viewBox and scaled by CSS. No dependency, crisp at any width. The palette
 * comes from the theme so both light and dark render -- a fill hardcoded
 * for one is the classic unreadable-illustration bug.
 */
import React from 'react'
import { useTheme } from '@mui/material/styles'
import { Box } from '@mui/material'

export type ArtKind = 'setup' | 'seed' | 'migrate'

const ART_LABEL: Record<ArtKind, string> = {
  setup: 'A tenant, with the project and credentials that get created alongside it',
  seed: 'Fabricated content falling into a single tenant',
  migrate: 'Content sweeping from one tenant into another, in one direction only',
}

export const WizardArt: React.FC<{ kind: ArtKind }> = ({ kind }) => {
  const t = useTheme()
  const dark = t.palette.mode === 'dark'
  const c = {
    // The card faces sit ON the aside panel, which is `paper` in dark mode
    // -- so `paper` here would make them invisible against it. One step
    // further from the page in each direction.
    card: dark ? '#35363a' : t.palette.background.paper,
    edge: dark ? 'rgba(255,255,255,0.10)' : 'rgba(0,0,0,0.06)',
    accent: t.palette.primary.main,
    soft: t.palette.primary.light,
    glow: dark ? 0.34 : 0.20,
    shadow: dark ? 0.5 : 0.14,
  }
  const uid = `wa-${kind}`

  /** A card face. No text: the composition carries it. */
  const Card = ({ x, y, w = 132, h = 96, o = 1, lines = 3 }:
                { x: number; y: number; w?: number; h?: number; o?: number; lines?: number }) => (
    <g opacity={o}>
      <rect x={x} y={y} width={w} height={h} rx={14} fill={c.card}
            stroke={c.edge} strokeWidth={1} filter={`url(#${uid}-sh)`} />
      <rect x={x + 16} y={y + 18} width={w * 0.42} height={7} rx={3.5} fill={c.accent} />
      {Array.from({ length: lines }).map((_, i) => (
        <rect key={i} x={x + 16} y={y + 38 + i * 14} rx={3}
              width={w - 32 - (i === lines - 1 ? 28 : 0)} height={6}
              fill={c.accent} opacity={0.18} />
      ))}
    </g>
  )

  return (
    <Box sx={{ mb: 3, mx: -1 }}>
      <svg viewBox="0 0 560 260" role="img" width="100%"
           style={{ height: 'auto', display: 'block' }}
           aria-label={ART_LABEL[kind]}>
        <defs>
          <linearGradient id={`${uid}-g`} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor={c.accent} stopOpacity="0.85" />
            <stop offset="100%" stopColor={c.soft} stopOpacity="0.15" />
          </linearGradient>
          <linearGradient id={`${uid}-sweep`} x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor={c.accent} stopOpacity="0" />
            <stop offset="45%" stopColor={c.accent} stopOpacity="0.65" />
            <stop offset="100%" stopColor={c.accent} stopOpacity="0.9" />
          </linearGradient>
          <filter id={`${uid}-blur`} x="-40%" y="-40%" width="180%" height="180%">
            <feGaussianBlur stdDeviation="34" />
          </filter>
          <filter id={`${uid}-sh`} x="-30%" y="-30%" width="160%" height="180%">
            <feDropShadow dx="0" dy="6" stdDeviation="10"
                          floodColor="#000" floodOpacity={c.shadow} />
          </filter>
        </defs>

        {/* The atmosphere: one soft field of colour, placed where the eye
            should land first for this step. */}
        <ellipse cx={kind === 'migrate' ? 280 : kind === 'seed' ? 280 : 190}
                 cy={130} rx={190} ry={110}
                 fill={`url(#${uid}-g)`} opacity={c.glow}
                 filter={`url(#${uid}-blur)`} />

        {kind === 'migrate' && (
          <>
            {/* One direction, stated by the gradient itself: it fades in at
                the source and arrives solid at the destination. */}
            <path d="M 150 196 C 250 214, 320 200, 430 156" fill="none"
                  stroke={`url(#${uid}-sweep)`} strokeWidth={3} strokeLinecap="round" />
            <Card x={44} y={74} o={0.55} w={120} h={86} lines={2} />
            <Card x={62} y={58} />
            <Card x={370} y={92} o={0.55} w={120} h={86} lines={2} />
            <Card x={352} y={76} />
            {[0, 1, 2].map((i) => (
              <circle key={i} cx={228 + i * 46} cy={196 - i * 14} r={5 - i * 0.6}
                      fill={c.accent} opacity={0.75 - i * 0.18} />
            ))}
          </>
        )}

        {kind === 'seed' && (
          <>
            {[0, 1, 2, 3].map((i) => (
              <rect key={i} x={196 + i * 46} y={20 + (i % 2) * 14}
                    width={26} height={26} rx={8}
                    fill={c.accent} opacity={0.22 + i * 0.12} />
            ))}
            {[0, 1, 2, 3].map((i) => (
              <line key={i} x1={209 + i * 46} y1={54 + (i % 2) * 14}
                    x2={209 + i * 46} y2={92} stroke={c.accent}
                    strokeWidth={2} strokeLinecap="round"
                    opacity={0.35} strokeDasharray="2 7" />
            ))}
            <Card x={214} y={104} w={132} h={104} lines={4} />
          </>
        )}

        {kind === 'setup' && (
          <>
            <Card x={70} y={62} w={150} h={116} lines={4} />
            <rect x={300} y={70} width={168} height={54} rx={14} fill={c.card}
                  stroke={c.edge} filter={`url(#${uid}-sh)`} />
            <circle cx={330} cy={97} r={12} fill={c.soft} />
            <rect x={352} y={90} width={86} height={7} rx={3.5} fill={c.accent} opacity={0.55} />
            <rect x={352} y={104} width={58} height={6} rx={3} fill={c.accent} opacity={0.2} />
            <rect x={300} y={140} width={168} height={54} rx={14} fill={c.card}
                  stroke={c.edge} filter={`url(#${uid}-sh)`} />
            <circle cx={330} cy={167} r={12} fill={c.soft} />
            <rect x={352} y={160} width={72} height={7} rx={3.5} fill={c.accent} opacity={0.55} />
            <rect x={352} y={174} width={94} height={6} rx={3} fill={c.accent} opacity={0.2} />
            <path d="M 228 118 C 262 112, 272 100, 292 97" fill="none"
                  stroke={c.accent} strokeWidth={2} opacity={0.4} strokeLinecap="round" />
            <path d="M 228 136 C 262 146, 272 162, 292 166" fill="none"
                  stroke={c.accent} strokeWidth={2} opacity={0.4} strokeLinecap="round" />
          </>
        )}
      </svg>
    </Box>
  )
}

export default WizardArt
