import type { CompletedJob } from '@/api/client'

export interface DomainRuns {
  domain: string
  runs: CompletedJob[]
}

/**
 * Finished runs, grouped by the tenant they acted on.
 *
 * Nothing records which tenant a run was for: a result is keyed on the job
 * NAME and the account, and an account has two tenants. So it is derived
 * the same way useRunningJobs derives it for a LIVE job -- anything naming
 * "target" acted on the target, everything else on the source -- rather
 * than by a second rule that could disagree with the running card sitting
 * directly above this list.
 *
 * Lives here rather than inside Jobs.tsx so its test exercises this code
 * instead of a copy of it. A test that reimplements the rule it is checking
 * passes while the real one is wrong, which is how a seeder bug survived a
 * suite that asserted on its source text.
 */
export function groupRunsByDomain(done: CompletedJob[], source?: string,
                                  target?: string): DomainRuns[] {
  const groups = new Map<string, CompletedJob[]>()
  for (const d of done) {
    const toTarget = /target/i.test(d.name)
    const domain = (toTarget ? target : source)
      // A run whose tenant is no longer configured still happened, and
      // dropping it would make the history lie by omission.
      || (toTarget ? 'target (not configured)' : 'source (not configured)')
    const list = groups.get(domain) ?? []
    list.push(d)
    groups.set(domain, list)
  }
  // Newest run first inside each tenant, and the tenant with the newest run
  // first overall -- the ordering the flat list had.
  return [...groups.entries()]
    .map(([domain, runs]) => ({
      domain,
      runs: [...runs].sort((a, b) => (b.finished ?? 0) - (a.finished ?? 0)),
    }))
    .sort((a, b) => (b.runs[0]?.finished ?? 0) - (a.runs[0]?.finished ?? 0))
}
