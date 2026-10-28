import { createContext, useContext } from 'react'
import type { Account, OwedGrants } from '@/api/controlPlane'
import type { ConfigPayload, HostInfo, JobStatus } from '@/api/client'
export interface WorkspaceState {
  account: Account | null
  config: ConfigPayload['config'] | null
  host: HostInfo | null
  job: JobStatus | null
  owed: OwedGrants['migrations']
}
export const WorkspaceContext = createContext<WorkspaceState | null>(null)
export const useWorkspaceContext = () => useContext(WorkspaceContext)
