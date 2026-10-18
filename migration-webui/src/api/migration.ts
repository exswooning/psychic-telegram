/**
 * Which migration this browser is working on.
 *
 * A login can own several migrations, each its own account on the server (its own
 * keys, ledger and jobs). Every request names the chosen one in X-Bitport-Migration;
 * both servers honour it only for a migration this login may open, and otherwise
 * answer about the login's own. Unset means "the login's own", as before.
 */
const KEY = 'bitport.migration'

export const chosenMigration = (): string => {
  try { return localStorage.getItem(KEY) || '' } catch { return '' }
}

export const chooseMigration = (id: number | null): void => {
  try {
    if (id == null) localStorage.removeItem(KEY)
    else localStorage.setItem(KEY, String(id))
  } catch { /* storage blocked: requests stay on the login's own migration */ }
  // The header names the migration in hand; the wizard switching it must not leave
  // that showing the previous one (live: gorkhatweed above an s. -> t. setup).
  window.dispatchEvent(new Event(CHANGED))
}

export const CHANGED = 'bitport:migration-changed'

/** One place for every request: client.ts, controlPlane.ts and pages that call
 *  fetch directly all go through window.fetch. Another tab switching reloads this
 *  one, so a page never shows one migration while asking about another. */
export function installMigrationHeader(): void {
  const original = window.fetch.bind(window)
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const id = chosenMigration()
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (!id || !url.includes('/api/')) return original(input, init)
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined))
    headers.set('X-Bitport-Migration', id)
    return original(input, { ...init, headers })
  }
  window.addEventListener('storage', (e) => { if (e.key === KEY) window.location.reload() })
}
