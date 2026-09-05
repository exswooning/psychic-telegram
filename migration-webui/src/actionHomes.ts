/**
 * Which page renders which action, and a catch-all for everything else.
 *
 * The lists on each page were hardcoded, so an action added to the backend
 * appeared nowhere until someone remembered to name it here too. Audited
 * live: 43 actions offered, 27 with a visible label, 16 reachable only by
 * typing a URL or using the legacy wizard -- including five wired that same
 * afternoon.
 *
 * The reachability test on the Python side did not catch it, because it
 * checks STEP_ACTIONS, which drives the old webui wizard rather than this
 * app. Two lists, one of them silently authoritative.
 *
 * So the fix is not a longer list. `unclaimed()` returns every action no
 * page has spoken for, and Maintenance renders those -- a new action shows
 * up somewhere by default, and the worst case is that it appears in a
 * slightly odd place rather than nowhere at all.
 */

/** Actions Verification renders: the read-only "is this right?" family. */
export const VERIFICATION_KEYS = [
  'verify', 'acl_audit', 'verify_ledger', 'ui_check',
  'verify_scopes_source', 'verify_scopes_target',
  'external_shares', 'external_shares_notify',
  'check_seed_accounts', 'check_seed_scopes',
]

/** Actions Maintenance renders explicitly, in a deliberate order. */
export const MAINTENANCE_KEYS = [
  'backup_now', 'backup_list',
  'audit_prune_dry', 'audit_prune',
  'resolve_dry', 'resolve',
  'repair_modified_times_dry', 'repair_modified_times', 'backfill_drive',
  'undo_dry', 'undo',
]

/** Keys other pages own, so the catch-all does not duplicate them. */
export const CLAIMED_ELSEWHERE = [
  // Services
  'shared_drives_inventory', 'shared_drives_migrate', 'staging_drives_cleanup',
  'sso_inventory', 'sso_migrate', 'reconcile', 'dms_import',
  // Mission Control / Wizard / Scope / Identities
  'migrate', 'discover', 'report', 'scope', 'export_scope',
  'init_db', 'init_db_auto', 'phased_migrate', 'phased_count_only',
  'reset_drive_ledger', 'inventory',
]

/**
 * Every action with no home. Rendered by Maintenance under "Everything else",
 * so a capability can never again exist on the server and nowhere on screen.
 */
export function unclaimed(keys: string[]): string[] {
  const spoken = new Set([
    ...VERIFICATION_KEYS, ...MAINTENANCE_KEYS, ...CLAIMED_ELSEWHERE,
  ])
  return keys.filter((k) => !spoken.has(k)).sort()
}
