import { useEffect, useRef, useCallback } from 'react'
import { useMigrationStore } from '@/store'
import { MetricPoint } from '@/types'
import {
  fetchUsers,
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

const useMigration = () => {
  const {
    users, setUsers, setMetrics, setActivities, setVerification, setReport,
    setLastUpdate, setError,
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
    try {
      const [users, metrics, activity, verification, report] = await Promise.all([
        fetchUsers(),
        fetchMetrics(),
        fetchActivity(),
        fetchVerification(),
        fetchReport(),
      ])
      history.current = [
        ...history.current.slice(-(HISTORY_POINTS - 1)),
        {
          timestamp: Date.now(),
          cpu: metrics.cpu,
          ram: metrics.ram.percentage,
          workers: metrics.workers.current,
          uploadQueue: metrics.uploadQueue,
          retryQueue: metrics.retryQueue,
        },
      ]
      setUsers(users)
      setMetrics({ ...metrics, history: history.current })
      setActivities(activity)
      setVerification(verification)
      if (report) setReport(report)
      setLastUpdate(new Date().toISOString())
      setError(null)
    } catch (e) {
      // Keep showing the last good data rather than blanking the dashboard
      // on one dropped poll -- a busy migration host can stall a single
      // request without anything actually being wrong.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      inFlight.current = false
    }
  }, [setUsers, setMetrics, setActivities, setVerification, setReport,
     setLastUpdate, setError])

  useEffect(() => {
    poll()
    intervalRef.current = setInterval(poll, POLL_MS)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [poll])

  return { users, setUsers, setMetrics }
}

export default useMigration