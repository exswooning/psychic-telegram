import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import ReopenUser from './ReopenUser'

const reopenUser = vi.fn()
vi.mock('@/api/controlPlane', () => ({ reopenUser: (...a: unknown[]) => reopenUser(...a) }))

const confirm = (reason: string) => {
  fireEvent.change(screen.getByLabelText('Reason Code'), { target: { value: reason } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
}

describe('run one user again', () => {
  beforeEach(() => reopenUser.mockReset())

  it('reopens only the services picked', async () => {
    reopenUser.mockResolvedValue({ ok: true, actionId: 1, detail: 'reopened gmail for a@src' })
    render(<ReopenUser email="a@src" />)
    fireEvent.click(screen.getByTestId('reopen-gmail'))
    expect(screen.getByTestId('reopen-go')).toHaveTextContent('Reopen gmail')
    fireEvent.click(screen.getByTestId('reopen-go'))
    confirm('mail really has more')
    await waitFor(() => expect(reopenUser).toHaveBeenCalledWith('a@src', ['gmail'], 'mail really has more', undefined))
    expect(await screen.findByText('reopened gmail for a@src')).toBeTruthy()
  })

  it('with nothing picked, reopens the whole user', async () => {
    reopenUser.mockResolvedValue({ ok: true, actionId: 1, detail: 'reopened a@src whole' })
    render(<ReopenUser email="a@src" />)
    expect(screen.getByTestId('reopen-go')).toHaveTextContent('Reopen the whole user')
    fireEvent.click(screen.getByTestId('reopen-go'))
    confirm('rerun everything')
    await waitFor(() => expect(reopenUser).toHaveBeenCalledWith('a@src', [], 'rerun everything', undefined))
  })
})
