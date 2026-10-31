export type FeatureGroup = 'Start' | 'Move' | 'Review' | 'Manage'
export interface WorkspaceFeature { path: string; label: string; description: string; group: FeatureGroup; operator?: boolean; seed?: boolean }
/** Each destination renders its existing, fully connected page. No duplicate job engine. */
export const workspaceFeatures: WorkspaceFeature[] = [
  { path: '/tools', label: 'All tools', description: 'Find every tool available to your account.', group: 'Manage' },
  { path: '/overview', label: 'Overview', description: 'Your migration at a glance and the next place to go.', group: 'Start' },
  { path: '/wizard', label: 'Setup Wizard', description: 'Connect tenants, credentials, scopes and account provisioning.', group: 'Start' },
  { path: '/migrations', label: 'Migrations', description: 'Tenant pairs, users, service progress and migration reports.', group: 'Move' },
  { path: '/jobs', label: 'Jobs', description: 'Run, queue, resume and stop work. Drive transfer methods and hybrid email.', group: 'Move' },
  { path: '/mirror', label: 'Mirror', description: 'Incremental sync, low priority background deltas and domain handover.', group: 'Move' },
  { path: '/mission-control', label: 'Mission Control', description: 'Detailed live controls, workers, quotas and migration stages.', group: 'Move' },
  { path: '/pipeline', label: 'Pipeline', description: 'Orchestrate discovery, migration, verification and repair stages.', group: 'Move' },
  { path: '/services', label: 'Other services', description: 'Mail, calendars, contacts, tasks, groups, sharing and Chat.', group: 'Move', operator: true },
  { path: '/users', label: 'People', description: 'Find users and open their per-service progress and findings.', group: 'Review' },
  { path: '/verification', label: 'Verification', description: 'Full or sampled comparisons, saved findings and repair controls.', group: 'Review' },
  { path: '/one-to-one', label: 'One-to-one', description: 'Source and target comparisons, content evidence and copy resolutions.', group: 'Review' },
  { path: '/tally', label: 'Tally', description: 'Source and target item inventories and count gaps.', group: 'Review' },
  { path: '/errors', label: 'Failures', description: 'Inspect errors, retries and unresolved repair findings.', group: 'Review' },
  { path: '/benchmarks', label: 'Benchmarks', description: 'Which transfer and mail mode is fastest and still ends one to one, with live progress and time left.', group: 'Review' },
  { path: '/metrics', label: 'Metrics', description: 'Transfer speed, API latency, quotas and capacity.', group: 'Review' },
  { path: '/activity', label: 'Activity', description: 'Live events and per-item migration activity.', group: 'Review' },
  { path: '/history', label: 'History', description: 'Previous runs, results and transcripts.', group: 'Review' },
  { path: '/report', label: 'Final Report', description: 'Migration totals, verification evidence and exportable reports.', group: 'Review' },
  { path: '/scope', label: 'Scope', description: 'Supported data types, coverage and documented limitations.', group: 'Review', operator: true },
  { path: '/settings', label: 'Settings', description: 'Transfer defaults, performance, connections and preferences.', group: 'Manage' },
  { path: '/system-health', label: 'System Health', description: 'Worker capacity, memory, disk and API health.', group: 'Manage' },
  { path: '/help', label: 'Help', description: 'Guides and help with the current migration.', group: 'Manage' },
  { path: '/identities', label: 'Identities', description: 'Source-to-target identity mappings and admin sign-ins.', group: 'Start', operator: true },
  { path: '/deploy', label: 'Deploy', description: 'Deployment configuration and worker installation.', group: 'Manage', operator: true },
  { path: '/nodes', label: 'Nodes', description: 'Join and manage migration workers.', group: 'Manage', operator: true },
  { path: '/maintenance', label: 'Maintenance', description: 'Guarded tenant maintenance and target cleanup.', group: 'Manage', operator: true },
  { path: '/logs', label: 'Logs', description: 'Operator logs, job output and diagnostics.', group: 'Manage', operator: true },
  { path: '/tests', label: 'Test suite', description: 'Run software checks and inspect their reports.', group: 'Manage', operator: true },
  { path: '/deadman', label: 'Safety monitor', description: 'Deadman safety controls and unattended operation.', group: 'Manage', operator: true },
  { path: '/gcp-teardown', label: 'GCP Teardown', description: 'Review and remove temporary Cloud project setup.', group: 'Manage', operator: true },
  { path: '/admin/accounts', label: 'Accounts (admin)', description: 'Bitport accounts, permissions and subscription access.', group: 'Manage', operator: true },
  { path: '/seed-wizard', label: 'Seed Wizard', description: 'Create synthetic sandbox data for migration trials.', group: 'Start', seed: true },
]
export const availableFeatures = (operator: boolean, seed: boolean) => workspaceFeatures.filter(f => (!f.operator || operator) && (!f.seed || seed))
export const findFeatures = (query: string, operator: boolean, seed: boolean) => {
  const words = query.toLowerCase().trim().split(/\s+/)
  return availableFeatures(operator, seed).filter(f => words.every(w => `${f.label} ${f.description} ${f.group}`.toLowerCase().includes(w)))
}
