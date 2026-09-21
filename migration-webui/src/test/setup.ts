// Extends expect() with the DOM matchers (toBeInTheDocument, etc.) and
// unmounts React trees between tests so one test's render can never be
// found by the next one's query.
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(cleanup)

// jsdom has no layout engine, so it never implemented ResizeObserver --
// recharts' ResponsiveContainer uses one to size a chart to its parent,
// and without this every test that renders one throws "ResizeObserver is
// not defined" before the component under test is even reached. A stub
// that reports nothing is correct here: no test in this suite asserts on
// a chart's pixel dimensions, only on the data and labels it renders.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverStub
