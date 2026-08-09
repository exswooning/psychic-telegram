export interface User {
  id: string
  name: string
  email: string
  status: MigrationStatus
  progress: number
  currentOperation: string
  estimatedTimeRemaining: string
  retries: number
  warnings: number
  errors: number
  lastUpdate: string
  details?: UserDetails
}

export interface UserDetails {
  mailbox: ServiceProgress
  calendar: ServiceProgress
  contacts: ServiceProgress
  drive: ServiceProgress
  chat: ServiceProgress
  permissions: ServiceProgress
  verification: ServiceProgress
}

export interface ServiceProgress {
  status: MigrationStatus
  progress: number
  itemsCompleted: number
  itemsTotal: number
  currentItem?: string
  speed?: string
  eta?: string
  details?: Record<string, any>
}

export type MigrationStatus = 
  | 'not_started'
  | 'waiting'
  | 'in_progress'
  | 'retrying'
  | 'completed'
  | 'failed'
  | 'needs_attention'
  | 'paused'
  | 'verified'
  | 'pending'
  | 'mismatch'

export interface MigrationStage {
  id: string
  name: string
  description: string
  status: MigrationStatus
  progress: number
  usersCompleted: number
  usersTotal: number
  expanded: boolean
}

export interface SystemMetrics {
  cpu: number
  ram: { used: number; total: number; percentage: number }
  disk: { used: number; total: number; percentage: number }
  network: { up: number; down: number }
  workers: { current: number; max: number; reason: string }
  uploadQueue: number
  retryQueue: number
  apiHealth: 'healthy' | 'degraded' | 'down'
  googleQuota: { used: number; limit: number; percentage: number }
  history: MetricPoint[]
}

export interface MetricPoint {
  timestamp: number
  cpu: number
  ram: number
  workers: number
  uploadQueue: number
  retryQueue: number
}

export interface ActivityEvent {
  id: string
  timestamp: string
  user: string
  action: string
  status: MigrationStatus
  details?: string
  // Only ever set on the one synthetic "System" row for the live background
  // job (see webui.py's _job_activity_entry()) -- null for every real
  // ledger-backed row, and null there too once the job stops running.
  progressPct?: number | null
  etaSeconds?: number | null
}

export interface VerificationResult {
  type: string
  status: 'verified' | 'mismatch' | 'pending' | 'not_started'
  sourceCount: number
  targetCount: number
  confidence: number
  // Only ever set on the "Share access" row: acl_audit.json is written by a
  // standalone script, never automatically during migrate/delta, so its
  // numbers can be stale relative to what is actually running right now.
  // Seconds since that file was last written; null for every ledger-backed
  // row, which is always live because it reads migration.db directly.
  ageSeconds?: number | null
}

export interface FinalReport {
  totalUsers: number
  successfulUsers: number
  failedUsers: number
  dataMigrated: string
  emailsMigrated: number
  driveFilesMigrated: number
  calendarEvents: number
  contacts: number
  groups: number
  sharedDrives: number
  totalDuration: string
  averageThroughput: string
  averageSpeed: string
  verificationSuccessRate: number
}

export interface HelpContext {
  whatIsHappening: string
  doINeedToDoAnything: string
  actionRequired?: string
  steps?: string[]
}