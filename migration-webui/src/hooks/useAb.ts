import { useCallback, useEffect, useState } from 'react'
import { AbView, fetchAbBenchmark } from '@/api/controlPlane'

export const fmtDuration = (s: number) => {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`
}

/** Polls the A/B view: every 5 s while an arm is running, 20 s otherwise. */
export function useAb(): AbView | null {
  const [view, setView] = useState<AbView | null>(null)
  const load = useCallback(() => {
    // A status read must never take a page down with it, whatever the API layer does.
    try { fetchAbBenchmark().then(setView).catch(() => {}) } catch { /* unavailable */ }
  }, [])
  useEffect(() => {
    load()
    const id = setInterval(load, view?.running ? 5_000 : 20_000)
    return () => clearInterval(id)
  }, [load, view?.running])
  return view
}
