/**
 * Full screen: the browser's own API where it is allowed, and a window-covering
 * overlay where it is not (unsupported, or refused). Either way the button says
 * which way it will go, and there is a way back that does not need the mouse.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import NodeGraph from './NodeGraph'

const view = () => render(
  <NodeGraph nodes={[]} edges={[]} frames={[]} size={{ w: 400, h: 300 }} viewHeight="72vh" label="graph" />)

const graph = () => screen.getByTestId('node-graph')
const covering = () => getComputedStyle(graph()).position === 'fixed'

let fsElement: Element | null = null
const setFullscreenElement = (el: Element | null) => {
  fsElement = el
  Object.defineProperty(document, 'fullscreenElement', { configurable: true, get: () => fsElement })
}

beforeEach(() => setFullscreenElement(null))
afterEach(() => {
  delete (HTMLElement.prototype as { requestFullscreen?: unknown }).requestFullscreen
  delete (document as { exitFullscreen?: unknown }).exitFullscreen
})

describe('NodeGraph full screen, without the browser API', () => {
  it('offers it, and starts not covering the page', () => {
    view()
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeInTheDocument()
    expect(covering()).toBe(false)
  })

  it('covers the window instead, and the button turns into the way out', () => {
    view()
    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    expect(covering()).toBe(true)
    expect(screen.getByRole('button', { name: 'Exit full screen' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Exit full screen' }))
    expect(covering()).toBe(false)
  })

  it('leaves on Escape, which the overlay has to handle itself', () => {
    view()
    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(covering()).toBe(false)
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeInTheDocument()
  })

  it('ignores Escape when it is not full screen', () => {
    view()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(covering()).toBe(false)
  })
})

describe('NodeGraph full screen, with the browser API', () => {
  const grant = () => {
    const request = vi.fn(function (this: Element) { setFullscreenElement(this); return Promise.resolve() })
    const exit = vi.fn(() => { setFullscreenElement(null); return Promise.resolve() })
    ;(HTMLElement.prototype as { requestFullscreen?: unknown }).requestFullscreen = request
    ;(document as { exitFullscreen?: unknown }).exitFullscreen = exit
    return { request, exit }
  }

  it('asks the browser to full-screen the graph itself, not the page', async () => {
    const { request } = grant()
    view()
    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1))
    expect(fsElement).toBe(graph())          // the mock records what it was called on
  })

  it('hands back to the browser when the button is pressed again', async () => {
    const { exit } = grant()
    view()
    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    await screen.findByRole('button', { name: 'Exit full screen' })
    await act(async () => { await Promise.resolve() })
    fireEvent.click(screen.getByRole('button', { name: 'Exit full screen' }))
    expect(exit).toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeInTheDocument()
  })

  it('follows the browser out when the user leaves with Esc', async () => {
    grant()
    view()
    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    await screen.findByRole('button', { name: 'Exit full screen' })
    await act(async () => { await Promise.resolve() })
    act(() => { setFullscreenElement(null); document.dispatchEvent(new Event('fullscreenchange')) })
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeInTheDocument()
    expect(covering()).toBe(false)
  })

  it('leaves the exit to the browser: an Esc the page happens to see does not desync the button', async () => {
    /* Chrome consumes Esc to leave full screen, but a key event can still reach
       the page (automation, some browsers). While the browser is in full screen
       the button must not claim otherwise. */
    grant()
    view()
    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    await screen.findByRole('button', { name: 'Exit full screen' })
    await act(async () => { await Promise.resolve() })
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(document.fullscreenElement).toBe(graph())
    expect(screen.getByRole('button', { name: 'Exit full screen' })).toBeInTheDocument()
  })

  it('falls back to covering the window when the browser refuses', async () => {
    const refuse = vi.fn(() => Promise.reject(new Error('not allowed')));
    (HTMLElement.prototype as { requestFullscreen?: unknown }).requestFullscreen = refuse
    view()
    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    await act(async () => { await Promise.resolve() })
    expect(screen.getByRole('button', { name: 'Exit full screen' })).toBeInTheDocument()
    expect(covering()).toBe(true)
    fireEvent.keyDown(window, { key: 'Escape' })       // no browser Esc here: the overlay handles it
    expect(covering()).toBe(false)
  })
})
