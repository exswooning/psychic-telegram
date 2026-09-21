import { render } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import ProgressSparkline from './ProgressSparkline'

describe('a trend needs at least two points', () => {
  it('renders nothing for zero samples', () => {
    const { container } = render(<ProgressSparkline samples={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for one sample -- a single point has no trend', () => {
    const { container } = render(<ProgressSparkline samples={[{ t: 0, pct: 10 }]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders a chart once there are two or more', () => {
    const { container } = render(
      <ProgressSparkline samples={[{ t: 0, pct: 10 }, { t: 5, pct: 40 }]} />)
    expect(container.querySelector('.recharts-responsive-container')).toBeInTheDocument()
  })
})
