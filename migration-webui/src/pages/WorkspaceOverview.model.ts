import type { User, ServiceProgress, VerificationResult } from '@/types'
export const serviceKeys = ['drive', 'mailbox', 'calendar', 'contacts', 'tasks', 'chat', 'permissions'] as const
export type ServiceKey = typeof serviceKeys[number]
export const serviceLabels: Record<ServiceKey, string> = { drive: 'Drive', mailbox: 'Gmail', calendar: 'Calendar', contacts: 'Contacts', tasks: 'Tasks', chat: 'Chat', permissions: 'Sharing' }
export const problemStatuses = ['failed', 'needs_attention', 'mismatch', 'paused']
const tokens: Record<ServiceKey, string[]> = { drive: ['drive'], mailbox: ['gmail', 'mailbox', 'mail'], calendar: ['calendar'], contacts: ['contact'], tasks: ['task'], chat: ['chat'], permissions: ['share', 'permission', 'access'] }
export const checksFor = (key: ServiceKey, checks: VerificationResult[]) => checks.filter(c => tokens[key].some(t => c.type.toLowerCase().includes(t)))
export function serviceRows(users: User[], key: ServiceKey) {
  return users.flatMap(user => {
    const progress = (user.details as unknown as Partial<Record<ServiceKey, ServiceProgress>> | undefined)?.[key]
    return progress ? [{ user, progress }] : []
  })
}
export function summarizeService(users: User[], key: ServiceKey, checks: VerificationResult[]) {
  const rows = serviceRows(users, key)
  const safe = (n: number) => Number.isFinite(n) ? Math.max(0, n) : 0
  const copied = rows.reduce((sum, r) => sum + safe(r.progress.itemsCompleted), 0)
  const total = rows.reduce((sum, r) => sum + safe(r.progress.itemsTotal), 0)
  const findings = checksFor(key, checks).filter(c => c.status === 'mismatch')
  const attention = rows.some(r => problemStatuses.includes(r.progress.status)) || findings.length > 0
  const complete = rows.length > 0 && rows.every(r => ['completed', 'verified'].includes(r.progress.status))
  const status = !rows.length ? 'Not reported' : attention ? 'Needs review' : complete ? 'Copied as reported' : rows.some(r => r.progress.status === 'in_progress' || r.progress.status === 'retrying') ? 'In progress' : copied ? 'Partly copied' : 'Not started'
  return { key, rows, copied, total, attention, complete, status, findings }
}
/** A ledger progress figure, never a fidelity or storage parity score. */
export function ledgerProgress(users: User[]): number | null {
  if (!users.length) return null
  const avg = users.reduce((sum, u) => sum + (Number.isFinite(u.progress) ? Math.max(0, Math.min(100, u.progress)) : 0), 0) / users.length
  return users.every(u => ['completed', 'verified'].includes(u.status) && u.progress >= 100) ? 100 : Math.min(99, Math.floor(avg))
}
