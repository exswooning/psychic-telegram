import React from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from '@/components/Layout'
import Wizard from '@/pages/Wizard'
import SeedWizard from '@/pages/SeedWizard'
import Dashboard from '@/pages/Dashboard'
import FleetDashboard from '@/pages/FleetDashboard'
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

const App: React.FC = () => {
  // Mounted once, at the root, so every routed page shares one poll loop
  // rather than each page starting (and losing) its own on navigation.
  // Previously this hook existed but nothing called it anywhere in the app --
  // confirmed by grep: zero references outside its own file -- so even the
  // fabricated Math.random() progress it used to generate never actually ran.
  useMigration()

  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
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
  )
}

export default App