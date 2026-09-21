import { useEffect, useRef, useState } from 'react'

export interface ProgressSample {
  t: number    // seconds since this browser tab started watching
  pct: number
}

const MAX_SAMPLES = 60

/**
 * A rolling window of a running job's own reported percentage, sampled as
 * it actually changes.
 *
 * Nothing here is estimated: every point is a `pct` the job itself
 * reported, at the moment this tab observed it. There is no server-side
 * time series to read instead -- job_admission and the fleet report only
 * the current instant -- so this is "what has been seen this session",
 * not a measurement the run itself kept. It resets on reload and on
 * switching jobs (keyed by job key), same honesty as everything else in
 * this codebase labelled "observed" rather than reported.
 */
export function useProgressHistory(key: string | undefined,
                                   pct: number | null | undefined): ProgressSample[] {
  const [samples, setSamples] = useState<ProgressSample[]>([])
  const startRef = useRef<number>(Date.now())
  const keyRef = useRef<string | undefined>(key)
  const lastPctRef = useRef<number | null>(null)

  useEffect(() => {
    if (keyRef.current !== key) {
      keyRef.current = key
      startRef.current = Date.now()
      lastPctRef.current = null
      setSamples([])
    }
  }, [key])

  useEffect(() => {
    if (typeof pct !== 'number') return
    // Only a genuine change is a sample -- a flat run polled every few
    // seconds should not draw as a staircase of identical points.
    if (lastPctRef.current === pct) return
    lastPctRef.current = pct
    const t = (Date.now() - startRef.current) / 1000
    setSamples((prev) => {
      const next = [...prev, { t, pct }]
      return next.length > MAX_SAMPLES ? next.slice(next.length - MAX_SAMPLES) : next
    })
  }, [pct])

  return samples
}
