import { getCpBase, getOperator } from './controlPlane'

export interface MicrosoftConnection { tenantId: string; clientId: string; cloud: string; region: string }
export interface MicrosoftConfig { source: MicrosoftConnection; target: MicrosoftConnection; mode: 'preserve_source' | 'native_move'; sandbox: boolean }
export interface MicrosoftRun { id: string; kind: string; state: string; progress: number; created_at: string; updated_at: string; detail: Record<string, unknown> }
export interface MicrosoftIdentity { sourceId: string; sourceName: string; sourceEmail: string; userType: string; targetId: string | null; targetEmail: string | null; confirmed: boolean; suggestedTargetId: string | null; profileState: string; differences: { field: string; source: unknown; target: unknown }[] }
export interface MicrosoftWorkload { id: string; label: string; proposedMethod: string; discoveryImplemented: boolean; transferQualified: boolean; sourceChanges: boolean; note: string; documentation: string; readPermissions: string[]; blockers: string[]; verification: string; inventory: Record<string, { state: string; records: number; scopes: number; incompleteScopes: number }> }
export interface MicrosoftFinding { side: string; workload: string; scope: string; state: string; count: number | null; detail: string; checked_at: string }
export interface MicrosoftWorkspace {
  accountId: number; config: MicrosoftConfig; runs: MicrosoftRun[]; sandboxWritesEnabled: boolean;
  credentials: Record<string, { ready: boolean; detail: string; expiresAt?: string }>;
  repairs: { operation_key: string; kind: string; state: string; detail: Record<string, unknown> }[];
  receipts: { operation_key: string; kind: string; source_id: string; target_id: string | null; state: string; updated_at: string; detail: Record<string, unknown> }[];
  plan: { inventoryId: string | null; inventoryCurrent: boolean; workloads: MicrosoftWorkload[]; identities: MicrosoftIdentity[];
    targetUsers: { id: string; email: string; name: string }[]; findings: MicrosoftFinding[]; findingCount?: number; findingsTruncated?: boolean; productionReady: boolean; locationVerified: boolean;
    summary: string; planHash: string; cutover: { ready: boolean; blockers: string[] } };
}
async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${getCpBase()}/api/v2/microsoft${path}`, {
    method: body === undefined ? 'GET' : 'POST', credentials: 'include',
    headers: { 'Content-Type': 'application/json', 'X-Operator': getOperator() },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try { const value = await response.json(); detail = typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail) } catch { /* keep HTTP error */ }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}
export const microsoftWorkspace = () => request<MicrosoftWorkspace>('/workspace')
export const microsoftSave = (config: MicrosoftConfig) => request('/connections', { ...config, reason: 'Configure Microsoft tenant connections' })
export const microsoftDiscover = () => request('/discover', { reason: 'Read both Microsoft tenants and record coverage gaps' })
export const microsoftMap = (sourceId: string, targetId: string) => request('/mappings', { sourceId, targetId, reason: 'Confirm immutable source and target identity mapping' })
export const microsoftStop = (id: string) => request(`/runs/${encodeURIComponent(id)}/stop`, { reason: 'Operator requested Microsoft job stop' })
export const microsoftRecover = (id: string) => request(`/runs/${encodeURIComponent(id)}/recover`, { reason: 'Reconcile an exited Microsoft worker without restarting it' })
export const microsoftPilot = (kind: 'profile' | 'drive', sourceId: string, planHash: string, confirm: string) => request(`/${kind}-pilot`, { sourceId, planHash, confirm, reason: 'Explicit sandbox target copy; source remains read-only' })
export async function microsoftExport() {
  const report = await request('/report')
  const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }))
  const a = document.createElement('a'); a.href = url; a.download = 'bitport-microsoft-coverage.json'; a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
