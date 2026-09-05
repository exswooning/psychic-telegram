/**
 * Collapse identical in-flight GETs onto a single request.
 *
 * Every polling surface in this app is `setInterval(fetchThing, N)` with no
 * check for whether the previous call has come back. That is harmless while
 * the response is faster than N and quietly corrosive when it is not: each
 * tick starts another copy, and the copies accumulate. The metrics page
 * polled /api/v2/metrics every 3s against a query measured at 3.16s, so it
 * was guaranteed to.
 *
 * The damage is not the wasted request. A browser opens at most six
 * concurrent connections per host, so a page holding several stalled copies
 * of one poll starves every other request behind it -- including the ones it
 * needs to render. That is what produced pages showing nothing but the nav,
 * intermittently, with no failed request to explain it: the requests had not
 * failed, they were still queued.
 *
 * Two callers already racing for the same URL would have got the same answer
 * anyway, so handing them one response changes nothing they could observe.
 * GET only, by construction -- coalescing writes would drop them.
 */
const inflight = new Map<string, Promise<unknown>>()

export function coalesce<T>(key: string, start: () => Promise<T>): Promise<T> {
  const hit = inflight.get(key) as Promise<T> | undefined
  if (hit) return hit
  const p = start().finally(() => { inflight.delete(key) })
  inflight.set(key, p)
  return p
}

/** Test seam: nothing in the app should need this. */
export const inflightCount = () => inflight.size
