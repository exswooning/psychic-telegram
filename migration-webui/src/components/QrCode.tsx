/**
 * A QR drawn from a matrix of modules, not from injected markup.
 *
 * One <rect> per dark module would be ~800 nodes; the whole grid is emitted
 * as a single path instead, which is one node and scales to any size
 * without resampling. shape-rendering: crispEdges matters more than it
 * looks -- antialiased module edges are what makes a phone camera fail on a
 * QR read off a screen.
 */
import React from 'react'

export const QrCode: React.FC<{ matrix: boolean[][]; size?: number }> = ({
  matrix, size = 264,
}) => {
  if (!matrix.length) return null
  const n = matrix.length
  const d = matrix.flatMap((row, y) =>
    row.map((on, x) => (on ? `M${x} ${y}h1v1h-1z` : '')).filter(Boolean),
  ).join('')
  return (
    <svg width={size} height={size} viewBox={`0 0 ${n} ${n}`}
         role="img" aria-label="Enrolment QR code" data-testid="qr"
         shapeRendering="crispEdges"
         style={{ background: '#fff', borderRadius: 4, display: 'block' }}>
      <path d={d} fill="#000" />
    </svg>
  )
}

export default QrCode
