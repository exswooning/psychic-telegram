// Extends expect() with the DOM matchers (toBeInTheDocument, etc.) and
// unmounts React trees between tests so one test's render can never be
// found by the next one's query.
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(cleanup)
