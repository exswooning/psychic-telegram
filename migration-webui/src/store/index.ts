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

const defaultUsers: User[] = [
  {
    id: '1',
    name: 'Alice Johnson',
    email: 'alice@c.anupam-poudel.com.np',
    status: 'in_progress',
    progress: 72,
    currentOperation: 'Migrating Gmail messages',
    estimatedTimeRemaining: '12 min',
    retries: 0,
    warnings: 1,
    errors: 0,
    lastUpdate: new Date().toISOString(),
    details: {
      mailbox: { status: 'completed', progress: 100, itemsCompleted: 156, itemsTotal: 156, currentItem: 'Complete' },
      calendar: { status: 'in_progress', progress: 65, itemsCompleted: 13, itemsTotal: 20, currentItem: 'Moving event "Weekly sync"' },
      contacts: { status: 'completed', progress: 100, itemsCompleted: 342, itemsTotal: 342, currentItem: 'Complete' },
      drive: { status: 'in_progress', progress: 45, itemsCompleted: 89, itemsTotal: 198, currentItem: 'Uploading PRJ-001-Apollo/Design.pptx' },
      chat: { status: 'waiting', progress: 0, itemsCompleted: 0, itemsTotal: 12 },
      permissions: { status: 'pending', progress: 0, itemsCompleted: 0, itemsTotal: 0, currentItem: 'Queued' },
      verification: { status: 'pending', progress: 0, itemsCompleted: 0, itemsTotal: 0, currentItem: 'Queued' },
    },
  },
  {
    id: '2',
    name: 'Bob Smith',
    email: 'bob@c.anupam-poudel.com.np',
    status: 'completed',
    progress: 100,
    currentOperation: 'Migration complete',
    estimatedTimeRemaining: 'Done',
    retries: 1,
    warnings: 0,
    errors: 0,
    lastUpdate: new Date().toISOString(),
    details: {
      mailbox: { status: 'completed', progress: 100, itemsCompleted: 89, itemsTotal: 89, currentItem: 'Complete' },
      calendar: { status: 'completed', progress: 100, itemsCompleted: 18, itemsTotal: 18, currentItem: 'Complete' },
      contacts: { status: 'completed', progress: 100, itemsCompleted: 210, itemsTotal: 210, currentItem: 'Complete' },
      drive: { status: 'completed', progress: 100, itemsCompleted: 156, itemsTotal: 156, currentItem: 'Complete' },
      chat: { status: 'completed', progress: 100, itemsCompleted: 8, itemsTotal: 8, currentItem: 'Complete' },
      permissions: { status: 'completed', progress: 100, itemsCompleted: 12, itemsTotal: 12, currentItem: 'Complete' },
      verification: { status: 'verified', progress: 100, itemsCompleted: 5, itemsTotal: 5, currentItem: 'All verified' },
    },
  },
  {
    id: '3',
    name: 'Carol Williams',
    email: 'carol@c.anupam-poudel.com.np',
    status: 'waiting',
    progress: 0,
    currentOperation: 'Waiting for mailbox scan',
    estimatedTimeRemaining: '~8 min',
    retries: 0,
    warnings: 0,
    errors: 0,
    lastUpdate: new Date().toISOString(),
    details: {
      mailbox: { status: 'not_started', progress: 0, itemsCompleted: 0, itemsTotal: 0, currentItem: 'Queued' },
      calendar: { status: 'not_started', progress: 0, itemsCompleted: 0, itemsTotal: 0, currentItem: 'Queued' },
      contacts: { status: 'not_started', progress: 0, itemsCompleted: 0, itemsTotal: 0, currentItem: 'Queued' },
      drive: { status: 'not_started', progress: 0, itemsCompleted: 0, itemsTotal: 0, currentItem: 'Queued' },
      chat: { status: 'not_started', progress: 0, itemsCompleted: 0, itemsTotal: 0, currentItem: 'Queued' },
      permissions: { status: 'not_started', progress: 0, itemsCompleted: 0, itemsTotal: 0, currentItem: 'Queued' },
      verification: { status: 'not_started', progress: 0, itemsCompleted: 0, itemsTotal: 0, currentItem: 'Queued' },
    },
  },
  {
    id: '4',
    name: 'Dave Chen',
    email: 'dave@c.anupam-poudel.com.np',
    status: 'retrying',
    progress: 34,
    currentOperation: 'Retrying Gmail label creation',
    estimatedTimeRemaining: '~22 min',
    retries: 2,
    warnings: 3,
    errors: 1,
    lastUpdate: new Date().toISOString(),
    details: {
      mailbox: { status: 'in_progress', progress: 50, itemsCompleted: 45, itemsTotal: 90, currentItem: 'Copying emails to Inbox' },
      calendar: { status: 'failed', progress: 0, itemsCompleted: 0, itemsTotal: 15, currentItem: 'Rate limited, retrying' },
      contacts: { status: 'completed', progress: 100, itemsCompleted: 178, itemsTotal: 178, currentItem: 'Complete' },
      drive: { status: 'in_progress', progress: 20, itemsCompleted: 34, itemsTotal: 170, currentItem: 'Uploading PRJ-004-Draco/Budget.xlsx' },
      chat: { status: 'waiting', progress: 0, itemsCompleted: 0, itemsTotal: 5 },
      permissions: { status: 'pending', progress: 0, itemsCompleted: 0, itemsTotal: 0, currentItem: 'Queued' },
      verification: { status: 'pending', progress: 0, itemsCompleted: 0, itemsTotal: 0, currentItem: 'Queued' },
    },
  },
  {
    id: '5',
    name: 'Erin Park',
    email: 'erin@c.anupam-poudel.com.np',
    status: 'needs_attention',
    progress: 15,
    currentOperation: 'Google quota exceeded',
    estimatedTimeRemaining: 'Paused',
    retries: 0,
    warnings: 1,
    errors: 1,
    lastUpdate: new Date().toISOString(),
    details: {
      mailbox: { status: 'in_progress', progress: 30, itemsCompleted: 12, itemsTotal: 40, currentItem: 'Copying emails' },
      calendar: { status: 'not_started', progress: 0, itemsCompleted: 0, itemsTotal: 10, currentItem: 'Queued' },
      contacts: { status: 'completed', progress: 100, itemsCompleted: 95, itemsTotal: 95, currentItem: 'Complete' },
      drive: { status: 'paused', progress: 5, itemsCompleted: 8, itemsTotal: 160, currentItem: 'Paused — quota limit' },
      chat: { status: 'not_started', progress: 0, itemsCompleted: 0, itemsTotal: 3 },
      permissions: { status: 'pending', progress: 0, itemsCompleted: 0, itemsTotal: 0, currentItem: 'Queued' },
      verification: { status: 'pending', progress: 0, itemsCompleted: 0, itemsTotal: 0, currentItem: 'Queued' },
    },
  },
]

const defaultStages: MigrationStage[] = [
  { id: 'discovery', name: 'Discovery', description: 'Scanning source tenant for data', status: 'completed', progress: 100, usersCompleted: 5, usersTotal: 5, expanded: false },
  { id: 'authentication', name: 'Authentication', description: 'Verifying OAuth tokens for both tenants', status: 'completed', progress: 100, usersCompleted: 5, usersTotal: 5, expanded: false },
  { id: 'verification', name: 'Verification', description: 'Validating source data integrity', status: 'completed', progress: 100, usersCompleted: 5, usersTotal: 5, expanded: false },
  { id: 'user_creation', name: 'User Creation', description: 'Creating target tenant user accounts', status: 'completed', progress: 100, usersCompleted: 5, usersTotal: 5, expanded: false },
  { id: 'gmail', name: 'Gmail Migration', description: 'Migrating emails, labels, and drafts', status: 'in_progress', progress: 68, usersCompleted: 2, usersTotal: 5, expanded: false },
  { id: 'drive', name: 'Drive Migration', description: 'Copying files, folders, and sharing permissions', status: 'in_progress', progress: 42, usersCompleted: 1, usersTotal: 5, expanded: false },
  { id: 'calendar', name: 'Calendar', description: 'Migrating events and calendars', status: 'waiting', progress: 0, usersCompleted: 0, usersTotal: 5, expanded: false },
  { id: 'contacts', name: 'Contacts', description: 'Migrating contact groups and entries', status: 'waiting', progress: 0, usersCompleted: 0, usersTotal: 5, expanded: false },
  { id: 'chat', name: 'Google Chat', description: 'Migrating chat messages and spaces', status: 'waiting', progress: 0, usersCompleted: 0, usersTotal: 5, expanded: false },
  { id: 'permissions', name: 'Permissions', description: 'Restoring ACLs, delegates, and sharing', status: 'waiting', progress: 0, usersCompleted: 0, usersTotal: 5, expanded: false },
  { id: 'validation', name: 'Validation', description: 'Verifying migrated data integrity', status: 'waiting', progress: 0, usersCompleted: 0, usersTotal: 5, expanded: false },
  { id: 'report', name: 'Final Report', description: 'Generating completion summary and exports', status: 'waiting', progress: 0, usersCompleted: 0, usersTotal: 5, expanded: false },
]

const defaultMetrics: SystemMetrics = {
  cpu: 23,
  ram: { used: 2450, total: 3815, percentage: 64 },
  disk: { used: 120, total: 500, percentage: 24 },
  network: { up: 4.2, down: 12.8 },
  workers: { current: 4, max: 8, reason: 'Memory healthy — scaling up workers' },
  uploadQueue: 12,
  retryQueue: 2,
  apiHealth: 'healthy',
  googleQuota: { used: 68, limit: 100, percentage: 68 },
  history: [
    { timestamp: Date.now() - 300000, cpu: 18, ram: 62, workers: 2, uploadQueue: 8, retryQueue: 1 },
    { timestamp: Date.now() - 240000, cpu: 22, ram: 63, workers: 3, uploadQueue: 10, retryQueue: 1 },
    { timestamp: Date.now() - 180000, cpu: 25, ram: 64, workers: 3, uploadQueue: 11, retryQueue: 2 },
    { timestamp: Date.now() - 120000, cpu: 21, ram: 63, workers: 4, uploadQueue: 12, retryQueue: 2 },
    { timestamp: Date.now() - 60000, cpu: 23, ram: 64, workers: 4, uploadQueue: 12, retryQueue: 2 },
    { timestamp: Date.now(), cpu: 23, ram: 64, workers: 4, uploadQueue: 12, retryQueue: 2 },
  ],
}

const defaultActivities: ActivityEvent[] = [
  { id: '1', timestamp: new Date(Date.now() - 30000).toISOString(), user: 'Alice Johnson', action: 'Copying emails to Gmail', status: 'in_progress', details: 'Processing batch 12 of 47' },
  { id: '2', timestamp: new Date(Date.now() - 45000).toISOString(), user: 'Bob Smith', action: 'Migration completed', status: 'completed', details: 'All services migrated successfully' },
  { id: '3', timestamp: new Date(Date.now() - 60000).toISOString(), user: 'Carol Williams', action: 'Starting mailbox scan', status: 'waiting', details: 'Discovery phase in progress' },
  { id: '4', timestamp: new Date(Date.now() - 90000).toISOString(), user: 'Dave Chen', action: 'Retrying label creation', status: 'retrying', details: 'HTTP 429 — rate limited, retry 2 of 3' },
  { id: '5', timestamp: new Date(Date.now() - 120000).toISOString(), user: 'Erin Park', action: 'Google quota exceeded', status: 'needs_attention', details: 'Drive API quota reached, pausing uploads' },
  { id: '6', timestamp: new Date(Date.now() - 180000).toISOString(), user: 'System', action: 'Worker scaling decision', status: 'completed', details: 'Memory healthy. Increasing workers from 2 to 4.' },
  { id: '7', timestamp: new Date(Date.now() - 240000).toISOString(), user: 'Alice Johnson', action: 'Restoring sharing permissions', status: 'in_progress', details: 'Processing ACL grants for 12 files' },
  { id: '8', timestamp: new Date(Date.now() - 300000).toISOString(), user: 'System', action: 'Discovery complete', status: 'completed', details: 'Found 5 users, 1,847 total items to migrate' },
]

const defaultVerification: VerificationResult[] = [
  { type: 'Emails', status: 'verified', sourceCount: 156, targetCount: 156, confidence: 99.8 },
  { type: 'Folders', status: 'verified', sourceCount: 68, targetCount: 68, confidence: 100 },
  { type: 'Attachments', status: 'verified', sourceCount: 342, targetCount: 342, confidence: 99.5 },
  { type: 'Calendar', status: 'mismatch', sourceCount: 20, targetCount: 18, confidence: 90 },
  { type: 'Drive', status: 'pending', sourceCount: 198, targetCount: 0, confidence: 0 },
  { type: 'Contacts', status: 'verified', sourceCount: 342, targetCount: 342, confidence: 100 },
  { type: 'Permissions', status: 'pending', sourceCount: 0, targetCount: 0, confidence: 0 },
  { type: 'Delegates', status: 'not_started', sourceCount: 0, targetCount: 0, confidence: 0 },
  { type: 'Groups', status: 'not_started', sourceCount: 0, targetCount: 0, confidence: 0 },
  { type: 'Shared Drives', status: 'not_started', sourceCount: 0, targetCount: 0, confidence: 0 },
]

const defaultReport: FinalReport = {
  totalUsers: 5,
  successfulUsers: 4,
  failedUsers: 1,
  dataMigrated: '2.4 GB',
  emailsMigrated: 345,
  driveFilesMigrated: 789,
  calendarEvents: 156,
  contacts: 890,
  groups: 12,
  sharedDrives: 3,
  totalDuration: '2h 14m',
  averageThroughput: '18.3 items/min',
  averageSpeed: '4.2 MB/s',
  verificationSuccessRate: 94.7,
}

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
      isLoading: false,
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