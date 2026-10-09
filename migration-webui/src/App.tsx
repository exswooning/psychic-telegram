import React, { Suspense, lazy, useEffect, useState } from 'react'
import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import Layout from '@/components/Layout'
import Login from '@/pages/Login'
import Signup from '@/pages/Signup'
import Pricing from '@/pages/Pricing'
import useMigration from '@/hooks/useMigration'
import { LinearProgress } from '@mui/material'
import { fetchMe, Account } from '@/api/controlPlane'

// Every page past sign-in is its own file, fetched the first time it is opened: the whole
// app was one 1.6 MB file that every visitor downloaded before the sign-in page drew.
const Wizard = lazy(() => import('@/pages/Wizard'))
const Jobs = lazy(() => import('@/pages/Jobs'))
const Pipeline = lazy(() => import('@/pages/Pipeline'))
const Nodes = lazy(() => import('@/pages/Nodes'))
const Migrations = lazy(() => import('@/pages/Migrations'))
const Mirror = lazy(() => import('@/pages/Mirror'))
const MigrationDetail = lazy(() => import('@/pages/MigrationDetail'))
const Metrics = lazy(() => import('@/pages/Metrics'))
const TestReport = lazy(() => import('@/pages/TestReport'))
const MissionControl = lazy(() => import('@/pages/MissionControl'))
const Users = lazy(() => import('@/pages/Users'))
const UserDetail = lazy(() => import('@/pages/UserDetail'))
const SystemHealth = lazy(() => import('@/pages/SystemHealth'))
const Verification = lazy(() => import('@/pages/Verification'))
const OneToOne = lazy(() => import('@/pages/OneToOne'))
const Tally = lazy(() => import('@/pages/Tally'))
const History = lazy(() => import('@/pages/History'))
const FinalReport = lazy(() => import('@/pages/FinalReport'))
const Settings = lazy(() => import('@/pages/Settings'))
const ActivityFeed = lazy(() => import('@/pages/ActivityFeed'))
const ErrorHandling = lazy(() => import('@/pages/ErrorHandling'))
const HelpSystem = lazy(() => import('@/pages/HelpSystem'))
const AdminAccounts = lazy(() => import('@/pages/AdminAccounts'))
const Deploy = lazy(() => import('@/pages/Deploy'))
const Identities = lazy(() => import('@/pages/Identities'))
const Maintenance = lazy(() => import('@/pages/Maintenance'))
const Services = lazy(() => import('@/pages/Services'))
const Scope = lazy(() => import('@/pages/Scope'))
const Logs = lazy(() => import('@/pages/Logs'))
const GcpTeardown = lazy(() => import('@/pages/GcpTeardown'))
const Deadman = lazy(() => import('@/pages/Deadman'))

// Routes reachable with no session at all -- a real signed-in account
// still lands on /login or /signup only via an explicit Navigate below,
// never gets stuck on them.
const PUBLIC_PATHS = ['/login', '/signup', '/pricing']

const App: React.FC = () => {
  // Mounted once, at the root, so every routed page shares one poll loop
  // rather than each page starting (and losing) its own on navigation.
  useMigration()

  const location = useLocation()
  // undefined = "haven't asked the server yet" (distinct from null, "asked
  // and there is no session") -- lets a first load on a protected route
  // hold rendering for one round trip instead of flashing the app shell
  // and then yanking it away the instant fetchMe() comes back 401.
  const [account, setAccount] = useState<Account | null | undefined>(undefined)

  // Checked once on mount, not on every navigation: this is a real network
  // round trip to api_server.py, and re-running it on every in-app click
  // would flash a loading state for no reason. Login.tsx and Signup.tsx
  // already know their own call succeeded before they navigate away from
  // themselves -- this check exists for "did I already have a session"
  // (a page refresh, a bookmark), not to re-verify every route change.
  useEffect(() => {
    fetchMe().then(setAccount).catch(() => setAccount(null))
  }, [])

  const isPublic = PUBLIC_PATHS.includes(location.pathname)

  if (account === undefined && !isPublic) {
    return null
  }

  if (!account && !isPublic) {
    return (
      <Routes>
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    )
  }

  return (
    <Routes>
      <Route path="/login" element={account ? <Navigate to="/mission-control" replace /> : <Login />} />
      <Route path="/signup" element={account ? <Navigate to="/mission-control" replace /> : <Signup />} />
      <Route path="/pricing" element={<Pricing />} />
      <Route path="/*" element={
        <Layout>
          <Suspense fallback={<LinearProgress />}>
            <Routes>
              <Route path="/" element={<Navigate to="/mission-control" replace />} />
              <Route path="/mission-control" element={<MissionControl />} />
              <Route path="/jobs" element={<Jobs />} />
              <Route path="/pipeline" element={<Pipeline />} />
              {/* Running Now is a label on the Jobs cards now, not a
                  destination. Redirected rather than removed so an old link,
                  a bookmark or a stale tab still lands somewhere useful. */}
              <Route path="/running-now" element={<Navigate to="/jobs" replace />} />
              <Route path="/nodes" element={<Nodes />} />
              <Route path="/migrations" element={<Migrations />} />
              <Route path="/migrations/:accountId" element={<MigrationDetail />} />
              <Route path="/mirror" element={<Mirror />} />
              <Route path="/metrics" element={<Metrics />} />
              <Route path="/migrations/:accountId/metrics" element={<Metrics />} />
              <Route path="/tests" element={<TestReport />} />
              <Route path="/wizard" element={<Wizard />} />
              {/* Setup Wizard and Seed Wizard merged into one doorway --
                 old bookmarks/links still land on the Seed path directly. */}
              <Route path="/seed-wizard" element={<Navigate to="/wizard?mode=seed" replace />} />
              <Route path="/users" element={<Users />} />
              <Route path="/users/:email" element={<UserDetail />} />
              <Route path="/system-health" element={<SystemHealth />} />
              <Route path="/verification" element={<Verification />} />
              <Route path="/one-to-one" element={<OneToOne />} />
              <Route path="/tally" element={<Tally />} />
              <Route path="/history" element={<History />} />
              <Route path="/report" element={<FinalReport />} />
              <Route path="/activity" element={<ActivityFeed />} />
              <Route path="/errors" element={<ErrorHandling />} />
              <Route path="/help" element={<HelpSystem />} />
              <Route path="/settings" element={<Settings />} />
              {/* Operator/superadmin-only: hidden from nav for a regular
                 account (see Layout.tsx's navItems), not route-blocked --
                 same UX-only-gate precedent as /admin/accounts below, the
                 real boundary is server-side (require_superadmin). */}
              <Route path="/deploy" element={<Deploy />} />
              <Route path="/identities" element={<Identities />} />
              <Route path="/maintenance" element={<Maintenance />} />
              <Route path="/services" element={<Services />} />
              <Route path="/scope" element={<Scope />} />
              <Route path="/logs" element={<Logs />} />
              <Route path="/gcp-teardown" element={<GcpTeardown />} />
              <Route path="/deadman" element={<Deadman />} />
              <Route path="/admin/accounts" element={<AdminAccounts />} />
            </Routes>
          </Suspense>
        </Layout>
      } />
    </Routes>
  )
}

export default App
