import React, { useEffect, useState } from 'react'
import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import Layout from '@/components/Layout'
import Login from '@/pages/Login'
import Signup from '@/pages/Signup'
import Pricing from '@/pages/Pricing'
import Wizard from '@/pages/Wizard'
import SeedWizard from '@/pages/SeedWizard'
import Dashboard from '@/pages/Dashboard'
import FleetDashboard from '@/pages/FleetDashboard'
import MissionControl from '@/pages/MissionControl'
import Users from '@/pages/Users'
import UserDetail from '@/pages/UserDetail'
import SystemHealth from '@/pages/SystemHealth'
import Verification from '@/pages/Verification'
import FinalReport from '@/pages/FinalReport'
import Settings from '@/pages/Settings'
import ActivityFeed from '@/pages/ActivityFeed'
import DriveMigration from '@/pages/DriveMigration'
import ErrorHandling from '@/pages/ErrorHandling'
import HelpSystem from '@/pages/HelpSystem'
import AdminAccounts from '@/pages/AdminAccounts'
import useMigration from '@/hooks/useMigration'
import { fetchMe, Account } from '@/api/controlPlane'

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
          <Routes>
            <Route path="/" element={<Navigate to="/mission-control" replace />} />
            <Route path="/mission-control" element={<MissionControl />} />
            <Route path="/wizard" element={<Wizard />} />
            <Route path="/seed-wizard" element={<SeedWizard />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/command" element={<FleetDashboard />} />
            <Route path="/users" element={<Users />} />
            <Route path="/users/:email" element={<UserDetail />} />
            <Route path="/drive" element={<DriveMigration />} />
            <Route path="/system-health" element={<SystemHealth />} />
            <Route path="/verification" element={<Verification />} />
            <Route path="/report" element={<FinalReport />} />
            <Route path="/activity" element={<ActivityFeed />} />
            <Route path="/errors" element={<ErrorHandling />} />
            <Route path="/help" element={<HelpSystem />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/admin/accounts" element={<AdminAccounts />} />
          </Routes>
        </Layout>
      } />
    </Routes>
  )
}

export default App
