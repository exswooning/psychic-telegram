/** "3 minutes ago", from an ISO stamp. Absolute times make an operator do
 *  timezone arithmetic to answer "has this been stuck?". */
export function ago(iso?: string): string {
  if (!iso) return ''
  const then = Date.parse(iso.endsWith('Z') ? iso : `${iso}Z`)
  if (Number.isNaN(then)) return ''
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`
  return `${Math.round(secs / 3600)}h ago`
}
