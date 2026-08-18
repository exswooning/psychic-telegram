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

const useMigration = () => {
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
    try {
      // allSettled, not all: this used to be one Promise.all, so a single
      // endpoint throwing (a report with no data yet, a transient 500 on a
      // busy host) rejected the WHOLE batch and skipped every setter --
      // users, stages, metrics, activity, verification, and report all
      // froze at their last good value together, silently, with no
      // indication any of it was stale. Confirmed live: Users kept showing
      // old data with nothing on screen to say a poll had ever failed.
      // Each domain now succeeds or fails independently, matching what a
      // human actually wants -- five live panels and one stale one beats
      // six stale panels because of one.
      const [users, stages, metrics, activity, verification, report] = await Promise.allSettled([
        fetchUsers(),
        fetchStages(),
        fetchMetrics(),
        fetchActivity(),
        fetchVerification(),
        fetchReport(),
      ])
      const failures: string[] = []
      const detail = (r: PromiseRejectedResult) =>
        r.reason instanceof Error ? r.reason.message : String(r.reason)

      if (users.status === 'fulfilled') setUsers(users.value)
      else failures.push(`users: ${detail(users)}`)

      if (stages.status === 'fulfilled') setStages(stages.value)
      else failures.push(`stages: ${detail(stages)}`)

      if (metrics.status === 'fulfilled') {
        history.current = [
          ...history.current.slice(-(HISTORY_POINTS - 1)),
          {
            timestamp: Date.now(),
            cpu: metrics.value.cpu,
            ram: metrics.value.ram.percentage,
            workers: metrics.value.workers.current,
            uploadQueue: metrics.value.uploadQueue,
            retryQueue: metrics.value.retryQueue,
          },
        ]
        setMetrics({ ...metrics.value, history: history.current })
      } else {
        failures.push(`metrics: ${detail(metrics)}`)
      }

      if (activity.status === 'fulfilled') setActivities(activity.value)
      else failures.push(`activity: ${detail(activity)}`)

      if (verification.status === 'fulfilled') setVerification(verification.value)
      else failures.push(`verification: ${detail(verification)}`)

      if (report.status === 'fulfilled') {
        if (report.value) setReport(report.value)
      } else {
        failures.push(`report: ${detail(report)}`)
      }

      setLastUpdate(new Date().toISOString())
      setError(failures.length ? failures.join('; ') : null)
    } finally {
      // isLoading only ever gates the *first* paint (see store/index.ts) --
      // it drops to false whether this poll succeeded or failed, so a down
      // backend shows the real error state instead of a spinner forever.
      setLoading(false)
      inFlight.current = false
    }
  }, [setUsers, setStages, setMetrics, setActivities, setVerification, setReport,
     setLastUpdate, setError, setLoading])

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