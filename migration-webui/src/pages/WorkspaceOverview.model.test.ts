import { describe, expect, it } from 'vitest'
import type { User } from '@/types'
import { ledgerProgress, summarizeService } from './WorkspaceOverview.model'
const user = (extra: Partial<User> = {}): User => ({ id: 'a', name: 'Alex', email: 'a@example.test', status: 'in_progress', progress: 99, currentOperation: '', estimatedTimeRemaining: '', retries: 0, warnings: 0, errors: 0, lastUpdate: '', ...extra })
describe('overview progress and evidence', () => {
  it('does not invent a percentage for an empty ledger', () => expect(ledgerProgress([])).toBeNull())
  it('does not round unfinished users to 100 percent', () => expect(ledgerProgress([user({ progress: 99.99 })])).toBe(99))
  it('requires completed statuses before displaying 100', () => {
    expect(ledgerProgress([user({ progress: 100, status: 'failed' })])).toBe(99)
    expect(ledgerProgress([user({ progress: 100, status: 'completed' })])).toBe(100)
  })
  it('keeps missing service data distinct from successful zero items', () => {
    expect(summarizeService([user()], 'drive', []).status).toBe('Not reported')
    const u = user({ details: { drive: { status: 'completed', progress: 100, itemsCompleted: 0, itemsTotal: 0 } } as User['details'] })
    expect(summarizeService([u], 'drive', []).status).toBe('Copied as reported')
  })
  it('a copied service retains a saved mismatch', () => {
    const u = user({ details: { drive: { status: 'completed', progress: 100, itemsCompleted: 3, itemsTotal: 3 } } as User['details'] })
    const s = summarizeService([u], 'drive', [{ type: 'Drive files', status: 'mismatch', sourceCount: 4, targetCount: 3, confidence: 75 }])
    expect(s.attention).toBe(true)
    expect(s.status).toBe('Needs review')
  })
})
