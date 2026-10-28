import { describe, expect, it } from 'vitest'
import { workspaceMotion } from './workspaceMotion'
import { workspaceTheme } from './workspace'
describe('workspace motion', () => {
  it('turns off animations and transitions when reduced motion is requested', () => {
    const reduced = workspaceMotion['@media (prefers-reduced-motion: reduce)']['*, *::before, *::after']
    expect(reduced.animation).toBe('none !important')
    expect(reduced.transition).toBe('none !important')
  })
  it('uses opacity and transforms for page entry', () => {
    expect(workspaceMotion['@keyframes workspaceEnter'].from).toEqual({ opacity: 0, transform: 'translateY(8px)' })
    expect(workspaceMotion['@keyframes workspaceEnter'].to).toEqual({ opacity: 1, transform: 'translateY(0)' })
  })
  it('ships the motion system in both themes', () => {
    for (const dark of [true, false]) expect(workspaceTheme(dark).components?.MuiCssBaseline?.styleOverrides).toHaveProperty('@keyframes workspaceEnter')
  })
})
