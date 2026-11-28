import React from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@/api/controlPlane', () => ({
  fetchRunPhases: vi.fn(async () => ({
    accountId: 5,
    record: {
      run: 'r1', startedAt: '2026-11-22T10:00:00Z', pid: 1, resumes: 1, resumedFrom: 'r0', state: 'STOPPED',
      resumeFrom: 'gmail',
      phases: [
        { seq: 1, name: 'drive', kind: 'pass', state: 'DONE', attempts: 1, startedAt: null, endedAt: null, note: null },
        { seq: 2, name: 'gmail', kind: 'pass', state: 'STOPPED', attempts: 1, startedAt: null, endedAt: null, note: null },
        { seq: 3, name: 'dms', kind: 'follow_on', state: 'PENDING', attempts: 0, startedAt: null, endedAt: null, note: null },
      ],
    },
  })),
}))

import RunPhasesCard from './RunPhasesCard'

describe('RunPhasesCard', () => {
  it('lists the phases in order and says where a start picks up', async () => {
    render(<RunPhasesCard accountId={5} />)
    await waitFor(() => expect(screen.getByTestId('run-phases')).toBeTruthy())
    expect(screen.getByTestId('run-phase-drive').textContent).toBe('Drive: done')
    expect(screen.getByTestId('run-phase-gmail').textContent).toBe('Mail: stopped')
    expect(screen.getByTestId('run-phase-dms').textContent).toBe('Google import: pending')
    expect(screen.getByTestId('run-phases-resume').textContent).toContain('picks up at Mail')
    expect(screen.getByTestId('run-phases-resume').textContent).toContain('Resumed 1 time.')
  })
})
