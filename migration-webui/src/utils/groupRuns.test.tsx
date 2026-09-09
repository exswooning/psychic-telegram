import { describe, it, expect } from 'vitest'
import type { CompletedJob } from '@/api/client'

/* Finished runs were a flat newest-first list, so "what has been done to
   this tenant" could only be answered by reading every card. Nothing
   records which tenant a run acted on -- a result is keyed on the job NAME
   and the account, and an account has two tenants -- so it is derived the
   same way useRunningJobs derives it for a live job, rather than by a
   second rule that could disagree with the running card above it. */

import { groupRunsByDomain as groupByDomain } from '@/utils/groupRuns'

const job = (name: string, finished: number): CompletedJob => ({
  runId: `${name}.${finished}`, name, rc: 0, finished, elapsed: 10,
  lineCount: 1, fromTranscript: false,
})

describe('finished runs, grouped by tenant', () => {
  it('puts source-side work under the source domain', () => {
    const g = groupByDomain([job('seed', 3), job('wipe tenant data', 2)],
                            'src.test', 'tgt.test')
    expect(g).toHaveLength(1)
    expect(g[0].domain).toBe('src.test')
    expect(g[0].runs).toHaveLength(2)
  })

  it('sends anything naming the target to the target', () => {
    const g = groupByDomain([job('seed', 3), job('reset target', 2)],
                            'src.test', 'tgt.test')
    expect(g.map((x) => x.domain).sort()).toEqual(['src.test', 'tgt.test'])
  })

  it('matches the rule the running card uses, case included', () => {
    /* useRunningJobs checks name.includes('target'); a disagreement would
       put a live job on one tenant and its own finished record on another. */
    const g = groupByDomain([job('Reset TARGET', 1)], 'src.test', 'tgt.test')
    expect(g[0].domain).toBe('tgt.test')
  })

  it('newest run first inside a tenant', () => {
    const g = groupByDomain([job('seed', 1), job('seed', 9), job('seed', 5)],
                            'src.test')
    expect(g[0].runs.map((r) => r.finished)).toEqual([9, 5, 1])
  })

  it('tenant with the newest run first overall', () => {
    const g = groupByDomain([job('seed', 1), job('reset target', 9)],
                            'src.test', 'tgt.test')
    expect(g[0].domain).toBe('tgt.test')
  })

  it('still shows a run whose tenant is no longer configured', () => {
    /* It happened. Hiding it would make the history lie by omission. */
    const g = groupByDomain([job('seed', 1)], undefined, undefined)
    expect(g[0].domain).toBe('source (not configured)')
    expect(g[0].runs).toHaveLength(1)
  })

  it('keeps two runs of one name apart', () => {
    const g = groupByDomain([job('seed', 1), job('seed', 2)], 'src.test')
    expect(g[0].runs.map((r) => r.runId)).toEqual(['seed.2', 'seed.1'])
  })
})
