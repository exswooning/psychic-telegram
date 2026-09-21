/**
 * Every point this hook produces is a real reported pct, sampled only when
 * it actually changes -- not a fabricated trend for a job that has not
 * moved. See the hook's own comment on why this is "observed this
 * session" rather than a measurement the job itself kept.
 */
import { renderHook } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { useProgressHistory } from './useProgressHistory'

describe('sampling only real changes', () => {
  it('starts with no history for a job that has not reported a number yet', () => {
    const { result } = renderHook(() => useProgressHistory('job-1', null))
    expect(result.current).toEqual([])
  })

  it('samples the first known pct, then each real change', () => {
    const { result, rerender } = renderHook(
      ({ pct }) => useProgressHistory('job-1', pct), { initialProps: { pct: 10 } })
    expect(result.current).toHaveLength(1)      // the starting value itself
    rerender({ pct: 25 })
    expect(result.current).toHaveLength(2)
    expect(result.current[1].pct).toBe(25)
  })

  it('does not add a second point for an unchanged pct', () => {
    const { result, rerender } = renderHook(
      ({ pct }) => useProgressHistory('job-1', pct), { initialProps: { pct: 10 } })
    rerender({ pct: 10 })
    rerender({ pct: 10 })
    expect(result.current).toHaveLength(1)       // only the initial sample
  })

  it('ignores a null pct rather than plotting it as zero', () => {
    const { result, rerender } = renderHook(
      ({ pct }) => useProgressHistory('job-1', pct),
      { initialProps: { pct: null as number | null } })
    rerender({ pct: null })
    expect(result.current).toEqual([])
  })

  it('resets when the job key changes', () => {
    const { result, rerender } = renderHook(
      ({ key, pct }: { key: string; pct: number }) => useProgressHistory(key, pct),
      { initialProps: { key: 'job-1', pct: 10 } })
    rerender({ key: 'job-1', pct: 50 })
    expect(result.current).toHaveLength(2)
    rerender({ key: 'job-2', pct: 5 })
    // The reset itself lands on the render after the key change; the new
    // job's first pct then samples on top of the cleared array.
    rerender({ key: 'job-2', pct: 20 })
    expect(result.current.every((s) => s.pct !== 50)).toBe(true)
  })

  it('caps the window so a long-running job does not grow forever', () => {
    const { result, rerender } = renderHook(
      ({ pct }) => useProgressHistory('job-1', pct), { initialProps: { pct: 0 } })
    for (let i = 1; i <= 100; i++) rerender({ pct: i })
    expect(result.current.length).toBeLessThanOrEqual(60)
  })
})
