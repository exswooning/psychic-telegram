import { useEffect, useRef, useCallback } from 'react'
import { useMigrationStore } from '@/store'
import { MetricPoint } from '@/types'
import {
  fetchUsers,
  fetchStages,
  fetchMetrics,
  fetchActivity,
  fetchVerification,
  fetchReport,
} from '@/api/client'

/**
 * Polls the real backend (webui.py / webui_spa.py) and pushes the result
 * into the Zustand store.
 *
 * This replaces a `setInterval` that called `Math.random()` on user progress
 * and system metrics -- there was no `fetch` anywhere in this file, or
 * anywhere else in `src/`. Every page reading `useMigrationStore` now reflects
 * the actual migration.db ledger, resources.py, and metrics.py.
 *
 * 4s, not faster: /api/spa/users runs tui.collect_snapshot() plus one grouped
 * SQL query per poll. Cheap against a SQLite ledger, but there is no reason to
 * poll harder than a human refreshes a dashboard by eye, and webui.py's own
 * STATUS_TTL pattern exists precisely because a read fired too often piles up
 * requests faster than they complete.
 */
const POLL_MS = 4000
const HISTORY_POINTS = 30

const useMigration = (enabled = true) => {
  const {
    users, setUsers, setStages, setMetrics, setActivities, setVerification,
    setReport, setLastUpdate, setError, setLoading,
  } = useMigrationStore()
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const inFlight = useRef(false)
  // The backend does not retain a time series (metrics.py's own RESERVOIR is
  // sized for a report, not a chart -- see its module docstring), so the
  // sparkline history is accumulated client-side from each real poll instead
  // of being fabricated in one shot.
  const history = useRef<MetricPoint[]>([])

  const poll = useCallback(async () => {
    // A slow response (a big ledger, a loaded host) must not stack a second
    // request on the first -- the same pile-up webui.py's STATUS_TTL comment
    // describes for the server side of this problem.
    if (inFlight.current) return
    inFlight.current = true
    // Each domain is applied THE MOMENT ITS OWN request returns, and the first paint is released by
    // the first one that does. This used to await all six and apply none until the slowest came
    // back, so one slow endpoint (a big ledger, a loaded host, a Cloudflare hiccup) left every page
    // on a loading bar with nothing in it, even for data that had arrived seconds earlier.
    const failures: Record<string, string> = {}
    const detail = (e: unknown) => (e instanceof Error ? e.message : String(e))
    const settle = <T,>(name: string, request: Promise<T>, apply: (v: T) => void) =>
      request.then(
        (v) => { apply(v); delete failures[name] },
        (e) => { failures[name] = `${name}: ${detail(e)}` },
      ).then(() => {
        setLoading(false)
        setLastUpdate(new Date().toISOString())
        setError(Object.keys(failures).length ? Object.values(failures).join('; ') : null)
      })
    try {
      await Promise.all([
        settle('users', fetchUsers(), setUsers),
        settle('stages', fetchStages(), setStages),
        settle('metrics', fetchMetrics(), (m) => {
          history.current = [
            ...history.current.slice(-(HISTORY_POINTS - 1)),
            {
              timestamp: Date.now(),
              cpu: m.cpu,
              ram: m.ram.percentage,
              workers: m.workers.current,
              uploadQueue: m.uploadQueue,
              retryQueue: m.retryQueue,
            },
          ]
          setMetrics({ ...m, history: history.current })
        }),
        settle('activity', fetchActivity(), setActivities),
        settle('verification', fetchVerification(), setVerification),
        settle('report', fetchReport(), (r) => { if (r) setReport(r) }),
      ])
    } finally {
      // isLoading only gates the first paint (see store/index.ts); each settled request above has
      // already released it, and this covers a request that never settled at all.
      setLoading(false)
      inFlight.current = false
    }
  }, [setUsers, setStages, setMetrics, setActivities, setVerification, setReport,
     setLastUpdate, setError, setLoading])

  useEffect(() => {
    if (!enabled) return
    poll()
    intervalRef.current = setInterval(poll, POLL_MS)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [poll, enabled])

  return { users, setUsers, setMetrics }
}

export default useMigration