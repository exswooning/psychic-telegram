import { create } from 'zustand'
import { devtools } from 'zustand/middleware'
import {
  User,
  MigrationStatus,
  MigrationStage,
  SystemMetrics,
  ActivityEvent,
  VerificationResult,
  FinalReport,
  HelpContext,
} from '@/types'

interface MigrationStore {
  users: User[]
  stages: MigrationStage[]
  metrics: SystemMetrics
  activities: ActivityEvent[]
  verification: VerificationResult[]
  report: FinalReport | null
  helpContext: HelpContext
  selectedUser: string | null
  sidebarOpen: boolean
  darkMode: boolean
  lastUpdate: string
  isLoading: boolean
  error: string | null
  setUsers: (users: User[]) => void
  updateUser: (email: string, updates: Partial<User>) => void
  setStages: (stages: MigrationStage[]) => void
  updateStage: (id: string, updates: Partial<MigrationStage>) => void
  setMetrics: (metrics: SystemMetrics) => void
  addActivity: (activity: ActivityEvent) => void
  setActivities: (activities: ActivityEvent[]) => void
  setVerification: (verification: VerificationResult[]) => void
  setReport: (report: FinalReport) => void
  setHelpContext: (context: HelpContext) => void
  setSelectedUser: (email: string | null) => void
  toggleSidebar: () => void
  toggleDarkMode: () => void
  setLoading: (loading: boolean) => void
  setError: (error: string | null) => void
  setLastUpdate: (time: string) => void
}

// Every one of these used to be hand-written, realistic-looking fake data
// (five named users with specific progress numbers, a frozen stage list,
// canned activity events) that rendered before the first real poll landed,
// and stayed on screen forever if that poll failed -- see useMigration.ts's
// note on Math.random(). This is the same fabrication problem in a static
// form instead of a random one: a `git blame` of "68%" pointed at nothing.
// The store now starts genuinely empty; isLoading gates the UI to show "no
// data yet" rather than a plausible-looking lie until the real fetch resolves.
const defaultUsers: User[] = []
const defaultStages: MigrationStage[] = []

const defaultMetrics: SystemMetrics = {
  cpu: 0,
  ram: { used: 0, total: 0, percentage: 0 },
  disk: { used: 0, total: 0, percentage: 0 },
  network: { up: 0, down: 0 },
  workers: { current: 0, max: 0, reason: 'Not yet polled' },
  uploadQueue: 0,
  retryQueue: 0,
  apiHealth: 'healthy',
  googleQuota: { used: 0, limit: 0, percentage: 0 },
  history: [],
}

const defaultActivities: ActivityEvent[] = []

const defaultVerification: VerificationResult[] = []

const defaultHelpContext: HelpContext = {
  whatIsHappening: 'The migration is currently copying Gmail messages from the source tenant to the target tenant. This is the largest data type and takes the most time.',
  doINeedToDoAnything: "No action is required. You can safely leave this running. The migration will automatically retry if Google rate-limits any API calls.",
}

export const useMigrationStore = create<MigrationStore>()(
  devtools(
    (set, get) => ({
      users: defaultUsers,
      stages: defaultStages,
      metrics: defaultMetrics,
      activities: defaultActivities,
      verification: defaultVerification,
      report: null,
      helpContext: defaultHelpContext,
      selectedUser: null,
      sidebarOpen: true,
      darkMode: false,
      lastUpdate: new Date().toISOString(),
      isLoading: true,
      error: null,
      setUsers: (users) => set({ users }, false, 'setUsers'),
      updateUser: (email, updates) =>
        set(
          (state) => ({
            users: state.users.map((u) => (u.email === email ? { ...u, ...updates, lastUpdate: new Date().toISOString() } : u)),
          }),
          false,
          'updateUser'
        ),
      setStages: (stages) => set({ stages }, false, 'setStages'),
      updateStage: (id, updates) =>
        set(
          (state) => ({
            stages: state.stages.map((s) => (s.id === id ? { ...s, ...updates } : s)),
          }),
          false,
          'updateStage'
        ),
      setMetrics: (metrics) => set({ metrics }, false, 'setMetrics'),
      addActivity: (activity) =>
        set(
          (state) => ({ activities: [activity, ...state.activities].slice(0, 100) }),
          false,
          'addActivity'
        ),
      setActivities: (activities) => set({ activities }, false, 'setActivities'),
      setVerification: (verification) => set({ verification }, false, 'setVerification'),
      setReport: (report) => set({ report }, false, 'setReport'),
      setHelpContext: (helpContext) => set({ helpContext }, false, 'setHelpContext'),
      setSelectedUser: (email) => set({ selectedUser: email }, false, 'setSelectedUser'),
      toggleSidebar: () => set((state) => ({ sidebarOpen: !state.sidebarOpen }), false, 'toggleSidebar'),
      toggleDarkMode: () => set((state) => ({ darkMode: !state.darkMode }), false, 'toggleDarkMode'),
      setLoading: (isLoading) => set({ isLoading }, false, 'setLoading'),
      setError: (error) => set({ error }, false, 'setError'),
      setLastUpdate: (lastUpdate) => set({ lastUpdate }, false, 'setLastUpdate'),
    }),
    { name: 'migration-store' }
  )
)