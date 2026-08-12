import React from 'react'
import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import Layout from '@/components/Layout'
import Login from '@/pages/Login'
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
import useMigration from '@/hooks/useMigration'
import { getOperator } from '@/api/controlPlane'

const App: React.FC = () => {
  // Mounted once, at the root, so every routed page shares one poll loop
  // rather than each page starting (and losing) its own on navigation.
  // Previously this hook existed but nothing called it anywhere in the app --
  // confirmed by grep: zero references outside its own file -- so even the
  // fabricated Math.random() progress it used to generate never actually ran.
  useMigration()

  // Forces this component to re-render on every navigation (React Router
  // context changes don't otherwise propagate up to a parent that isn't
  // itself calling a router hook) so that the getOperator() read below picks
  // up the name Login.tsx just wrote to localStorage, not a stale empty
  // string captured on first mount.
  const location = useLocation()
  const operator = getOperator()

  if (!operator && location.pathname !== '/login') {
    return (
      <Routes>
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    )
  }

  return (
    <Routes>
      <Route path="/login" element={operator ? <Navigate to="/mission-control" replace /> : <Login />} />
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
          </Routes>
        </Layout>
      } />
    </Routes>
  )
}

export default App