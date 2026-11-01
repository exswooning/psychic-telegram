/**
 * What a finished run printed last, as a person would say it.
 *
 * A repair ends by printing its whole summary as one JSON object, and the Verification and
 * History tables showed that object as text (live: `{"user": "finance@…", "services": ["drive",
 * "gmail", …], "state": "NEEDS_ATTENTION", "repairs": {…`). Anything that is not such an
 * object is returned untouched.
 */
type Repairs = Record<string, { changed?: number; errors?: unknown[] }>

export function plainRunDetail(detail: string | null | undefined): string {
  const text = (detail ?? '').trim()
  if (!text.startsWith('{')) return text
  let o: { user?: string; services?: unknown[]; state?: string; repairs?: Repairs }
  try { o = JSON.parse(text) } catch { return text }
  if (!o || typeof o !== 'object' || (!o.user && !o.state)) return text
  const repairs = Object.values(o.repairs ?? {})
  const fixed = repairs.reduce((n, r) => n + (r?.changed ?? 0), 0)
  const errors = repairs.reduce((n, r) => n + (r?.errors?.length ?? 0), 0)
  const parts = [
    o.user,
    o.state ? o.state.toLowerCase().replace(/_/g, ' ') : null,
    Array.isArray(o.services) ? `${o.services.length} service${o.services.length === 1 ? '' : 's'}` : null,
    repairs.length ? `${fixed} fixed${errors ? `, ${errors} error${errors === 1 ? '' : 's'}` : ''}` : null,
  ]
  return parts.filter(Boolean).join(' · ')
}
