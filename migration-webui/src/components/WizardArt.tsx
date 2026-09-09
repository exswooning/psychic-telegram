/**
 * The illustration beside each setup step.
 *
 * Google Workspace's signup puts a marketing render here -- a Gemini card,
 * a gradient sparkle, "Jump start your productivity with AI". We cannot
 * copy that honestly: there is no product shot to sell, and a decorative
 * flourish would be the filler the rest of this app avoids.
 *
 * So it draws the mechanism instead. Each step gets the picture of what is
 * actually about to happen to the tenant -- which fills the same visual
 * role, at the same scale, while telling the operator something they need:
 * that a migration READS the source and only ever WRITES the target, and
 * that a seed writes fabricated data into one tenant and touches nothing
 * else. That asymmetry is the single most consequential fact about pointing
 * this tool at a real company, and a diagram states it faster than the
 * paragraph underneath.
 *
 * Hand-authored SVG with native shapes, sized by viewBox and scaled by CSS,
 * so it stays crisp at any width and adds no dependency. Colours come from
 * the theme rather than literals, so both light and dark render correctly --
 * a fill hardcoded for one is the classic unreadable-illustration bug.
 */
import React from 'react'
import { useTheme } from '@mui/material/styles'
import { Box } from '@mui/material'

export type ArtKind = 'setup' | 'seed' | 'migrate'

/** The five per-user services this tool actually moves, as small glyphs.
 *  Real services, in the order the engine copies them -- not five decorative
 *  dots that happen to number five. */
const SERVICES = ['Drive', 'Gmail', 'Calendar', 'Contacts', 'Tasks']

const Tenant: React.FC<{
  x: number; y: number; label: string; c: Record<string, string>
  badge?: string; badgeTone?: 'read' | 'write'
}> = ({ x, y, label, c, badge, badgeTone }) => (
  <g>
    <rect x={x} y={y} width={200} height={168} rx={16}
          fill={c.surface} stroke={c.line} strokeWidth={1.5} />
    <text x={x + 20} y={y + 34} fontSize={13} fontWeight={500} fill={c.text}>
      {label}
    </text>
    <line x1={x + 20} y1={y + 48} x2={x + 180} y2={y + 48}
          stroke={c.line} strokeWidth={1} />
    {SERVICES.map((s, i) => (
      <g key={s}>
        <circle cx={x + 27} cy={y + 70 + i * 19} r={4} fill={c.accent} />
        <text x={x + 40} y={y + 74 + i * 19} fontSize={11} fill={c.dim}>{s}</text>
      </g>
    ))}
    {badge && (
      <g>
        <rect x={x + 112} y={y + 60} width={72} height={22} rx={11}
              fill={badgeTone === 'read' ? c.calmBg : c.warnBg} />
        <text x={x + 148} y={y + 75} fontSize={10} fontWeight={500}
              textAnchor="middle"
              fill={badgeTone === 'read' ? c.calm : c.warn}>{badge}</text>
      </g>
    )}
  </g>
)

export const WizardArt: React.FC<{ kind: ArtKind; source?: string; target?: string }> =
  ({ kind, source, target }) => {
    const t = useTheme()
    const c = {
      surface: t.palette.background.paper,
      line: t.palette.divider,
      text: t.palette.text.primary,
      dim: t.palette.text.secondary,
      accent: t.palette.primary.main,
      accentBg: t.palette.primary.light,
      calm: t.palette.success.main,
      calmBg: t.palette.success.light,
      warn: t.palette.warning.dark,
      warnBg: t.palette.warning.light,
    }
    const src = source || 'your tenant'
    const dst = target || 'the destination'

    return (
      <Box sx={{ mb: 3 }}>
        <svg viewBox="0 0 560 250" role="img" width="100%"
             style={{ height: 'auto', display: 'block' }}
             aria-label={
               kind === 'migrate'
                 ? `${src} is read and copied into ${dst}; the source is never written to`
                 : kind === 'seed'
                 ? `fabricated users, files and mail are written into ${src}`
                 : `a Cloud project and a delegated service account are created for ${src}`
             }>
          <defs>
            <marker id="wz-arrow" viewBox="0 0 10 10" refX="9" refY="5"
                    markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill={c.accent} />
            </marker>
          </defs>

          {kind === 'migrate' && (
            <>
              <Tenant x={16} y={44} label={src} c={c} badge="read-only" badgeTone="read" />
              <Tenant x={344} y={44} label={dst} c={c} badge="written" badgeTone="write" />
              <line x1={232} y1={128} x2={332} y2={128} stroke={c.accent}
                    strokeWidth={2} markerEnd="url(#wz-arrow)" />
              <text x={282} y={118} fontSize={11} fill={c.dim} textAnchor="middle">
                copy
              </text>
              <text x={282} y={150} fontSize={10} fill={c.dim} textAnchor="middle">
                per user
              </text>
              {/* The claim the whole page rests on, drawn rather than asserted:
                  nothing points back at the source. */}
              <text x={116} y={238} fontSize={10} fill={c.calm} textAnchor="middle">
                never written to
              </text>
            </>
          )}

          {kind === 'seed' && (
            <>
              <Tenant x={180} y={62} label={src} c={c} badge="written" badgeTone="write" />
              {[0, 1, 2].map((i) => (
                <g key={i}>
                  <rect x={236 + i * 36} y={16} width={22} height={22} rx={5}
                        fill={c.accentBg} stroke={c.accent} strokeWidth={1} />
                  <line x1={247 + i * 36} y1={40} x2={247 + i * 36} y2={58}
                        stroke={c.accent} strokeWidth={1.5}
                        markerEnd="url(#wz-arrow)" strokeDasharray="3 3" />
                </g>
              ))}
              <text x={280} y={244} fontSize={11} fill={c.dim} textAnchor="middle">
                fabricated users, files, mail and events
              </text>
            </>
          )}

          {kind === 'setup' && (
            <>
              <Tenant x={16} y={44} label={src} c={c} />
              <rect x={344} y={60} width={200} height={64} rx={12}
                    fill={c.surface} stroke={c.line} strokeWidth={1.5} />
              <text x={364} y={86} fontSize={12} fontWeight={500} fill={c.text}>
                Cloud project
              </text>
              <text x={364} y={104} fontSize={10} fill={c.dim}>
                created for this tenant only
              </text>
              <rect x={344} y={140} width={200} height={64} rx={12}
                    fill={c.surface} stroke={c.line} strokeWidth={1.5} />
              <text x={364} y={166} fontSize={12} fontWeight={500} fill={c.text}>
                Service account
              </text>
              <text x={364} y={184} fontSize={10} fill={c.dim}>
                acts for your users, once granted
              </text>
              <line x1={232} y1={104} x2={332} y2={92} stroke={c.accent}
                    strokeWidth={2} markerEnd="url(#wz-arrow)" />
              <line x1={232} y1={140} x2={332} y2={172} stroke={c.accent}
                    strokeWidth={2} markerEnd="url(#wz-arrow)" />
            </>
          )}
        </svg>
      </Box>
    )
  }

export default WizardArt
