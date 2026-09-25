/**
 * The whole system as data: what exists, where it runs, what feeds what.
 *
 * One source for the picture AND its explanations, so the graph cannot drift
 * from the descriptions -- and a test checks that every file named here really
 * exists, so it cannot drift from the repo either. Sockets are not listed
 * here: they are derived from the edges (see layout.ts), which is why adding a
 * wire is one line.
 *
 * Left to right is the order a migration happens in. A few wires run
 * backwards (a repair tool writing to the target it sits to the right of);
 * that is the system, not a layout accident.
 */
export type Kind = 'creds' | 'items' | 'map' | 'ctl'
export type Role = 'tenant' | 'access' | 'control' | 'engine' | 'store' | 'verify' | 'guard' | 'ui' | 'seed'
export type Where = 'VPS' | 'Worker node' | 'Google' | 'Browser' | 'Any machine'

export interface PFrame { id: string; title: string; band: number }

export interface PNode {
  id: string; frame: string; col: number; role: Role
  title: string
  /** Shown under the title -- short, so it fits the box. */
  sub: string
  where: Where
  /** What it is and why it is built the way it is. Plain words. */
  about: string
  /** Files in this repo that implement it. Checked by a test. */
  files: string[]
  /** A page in this app with more on it. */
  page?: string
  /** /api/spa/stages id whose counts this node shows live. */
  stage?: string
  /** Reserve a live-status row (a node whose live text arrives later must not
   *  shift the layout when it does). */
  live?: boolean
  /** Settings that tune it. */
  knobs?: string[]
}

export interface PEdge { from: string; to: string; label: string; kind: Kind }

export const FRAMES: PFrame[] = [
  { id: 'setup', title: 'Setup & access', band: 0 },
  { id: 'seeder', title: 'Rehearsal seeder', band: 0 },
  { id: 'guard', title: 'Guardrails', band: 1 },
  { id: 'source', title: 'Source tenant', band: 1 },
  { id: 'plan', title: 'Discover & plan', band: 2 },
  { id: 'control', title: 'Control', band: 3 },
  { id: 'pacing', title: 'Pacing & resilience', band: 4 },
  { id: 'engines', title: 'Engines', band: 5 },
  { id: 'ledger', title: 'Ledger', band: 6 },
  { id: 'target', title: 'Target tenant', band: 6 },
  { id: 'verify', title: 'Verify & repair', band: 7 },
  { id: 'watch', title: 'Watch', band: 8 },
  { id: 'tooling', title: 'Tooling', band: 8 },
]

const N = (
  id: string, frame: string, col: number, role: Role, title: string, sub: string,
  where: Where, about: string, files: string[], extra: Partial<PNode> = {},
): PNode => ({ id, frame, col, role, title, sub, where, about, files, ...extra })

export const NODES: PNode[] = [
  // ---- Setup & access ----------------------------------------------------
  N('wizard', 'setup', 0, 'access', 'Setup wizard', 'full_setup.py · wizard.py', 'VPS',
    'Creates the Cloud project, service account, key and delegation for a tenant, then saves its config. Drives a real browser for the Admin Console steps that have no API. A restart mid-run marks it interrupted; nothing is left half-changed that a re-run will not redo.',
    ['full_setup.py', 'wizard.py', 'gcloud_browser_auth.py'], { page: '/wizard' }),
  N('totp', 'setup', 0, 'access', '2-Step answers', 'totp.py · signin_challenge', 'VPS',
    'Answers Google\'s 2-Step during automated sign-in from a stored seed, and puts any on-screen prompt ("tap 47 on your phone") where a person can see it.',
    ['totp.py', 'signin_challenge.py', 'admin_secrets.py']),
  N('gcp', 'setup', 1, 'access', 'Cloud project & APIs', 'provision_gcp.py', 'Browser',
    'One Cloud project per tenant side, with the Drive, Gmail, Calendar, Chat and Admin SDK APIs enabled and a service account created. Each project carries its own API quota.',
    ['provision_gcp.py', 'ensure_apis.py', 'gcloud_browser_auth.py']),
  N('keys', 'setup', 1, 'access', 'Service-account keys', 'keys/<account>/*-sa.json', 'VPS',
    'The credential the engine acts with. Delegation is granted per service-account client ID, so copying a key file copies its live delegation with it. Auth mode is key, impersonate or oauth.',
    ['auth.py', 'accounts_auth.py'], { knobs: ['AUTH_MODE', 'SOURCE_SA_KEY', 'TARGET_SA_KEY'] }),
  N('dwd', 'setup', 2, 'access', 'Domain-wide delegation', 'dwd_helper · verify_scopes', 'Browser',
    'The Admin Console grant of scopes to the service account. Google has no API for it, so a browser does it. It can take up to ~15 minutes to propagate, and the only way to check is to mint a token per scope, which verify_scopes does.',
    ['dwd_helper.py', 'verify_scopes.py', 'repair_console_setup.py', 'reconnect_pack.py'],
    { stage: 'authentication', live: true }),
  N('tenantcfg', 'setup', 2, 'access', 'Tenant config', 'tenant_configs · per acct', 'VPS',
    'One row per (account, side): domain, admin, key path. Isolation between customers is by directory, not by column: each account has its own keys/ and its own ledger, so the engine never needed to know about accounts.',
    ['accounts_auth.py', 'control_plane_db.py', 'tenant_inventory.py']),

  // ---- Rehearsal seeder ---------------------------------------------------
  N('corpus', 'seeder', 0, 'seed', 'Corpus', 'corpus.py', 'VPS',
    'The fabricated organisation: departments, projects, people and the mail, documents and events that go with them, sized by scale (tiny to huge).',
    ['data-generator/corpus.py']),
  N('seed', 'seeder', 1, 'seed', 'Seeder', 'seed_sandbox.py', 'VPS',
    'Writes rehearsal data into a SOURCE sandbox tenant. Top-up mode only adds; fill mode adds filler up to a percentage of each account\'s licence share (storage is pooled, so the API\'s own limit is the whole tenant\'s). Refuses any domain that is not a declared sandbox.',
    ['data-generator/seed_sandbox.py', 'data-generator/seed_shared_drives.py', 'check_seed.py'],
    { page: '/wizard?mode=seed', knobs: ['--fill-percent', '--scale', '--top-up-only'] }),

  // ---- Guardrails ---------------------------------------------------------
  N('scopeguard', 'guard', 0, 'guard', 'Scope guard', 'scope_guard.py · scope.py', 'VPS',
    'Refuses to start a migration that would die on a missing scope, says exactly which scope on which tenant, and fixes it unattended when it can. scope.py is the 70-element matrix of what does and does not migrate.',
    ['scope_guard.py', 'scope.py'], { page: '/scope' }),
  N('domguard', 'guard', 0, 'guard', 'Domain guard', 'domain_guard.py', 'VPS',
    'Every domain a setup wizard has ever configured is protected from the seeder and from reset/wipe the moment its config is written. A domain must be explicitly declared a sandbox before it can be touched.',
    ['domain_guard.py']),

  // ---- Source tenant ------------------------------------------------------
  N('src', 'source', 0, 'tenant', 'Source tenant', 'Workspace · read side', 'Google',
    'The Google Workspace being left. A real migration only reads it (drive.readonly by default; server_side transfer needs full drive because files.copy is a create call made as the source user). Only the seeder writes here, and only on a sandbox.',
    []),

  // ---- Discover & plan ----------------------------------------------------
  N('disc', 'plan', 0, 'control', 'Discovery', 'main.py discover', 'VPS',
    'Read-only scan of the source: files and folders per user, tree depth, bytes, MIME mix and mail volume. Produces a duration estimate and a wall-clock floor from the 750 GB/user/day cap.',
    ['discovery.py', 'inventory.py'], { stage: 'discovery', live: true }),
  N('coverage', 'plan', 0, 'control', 'Coverage & readiness', 'coverage_audit.py', 'VPS',
    'Which supported data types the source actually contains, what survives downgrading the source tenant to Cloud Identity, and the next thing to do, counted against this tenant rather than described in general.',
    ['coverage_audit.py', 'cutover_readiness.py', 'next_actions.py']),
  N('idmap', 'plan', 1, 'store', 'Identity map', 'identity_map table', 'VPS',
    'Which source address becomes which target address (users and groups), from a reviewed CSV or by matching local parts (--auto-map). Everything downstream is bounded by it.',
    ['db.py', 'main.py'], { page: '/identities' }),
  N('provision', 'plan', 2, 'control', 'Provision accounts', 'provision.py', 'VPS',
    'Creates missing target accounts, only ever creating (an existing address is never renamed or given a new password). Accounts are made with changePasswordAtNextLogin=False because a pending password change silently breaks delegation. Needs a licence each.',
    ['provision.py'], { stage: 'user_creation', live: true, knobs: ['AUTO_PROVISION_USERS'] }),
  N('preflight', 'plan', 2, 'control', 'Preflight', 'preflight.py', 'VPS',
    'Mints a token and makes one call per user on both tenants, so a four-hour failure becomes a four-second one, and lists what will NOT migrate (too large, unexportable) before the run instead of hours into it.',
    ['preflight.py']),

  // ---- Control ------------------------------------------------------------
  N('ui', 'control', 0, 'ui', 'Web UI & API', 'webui.py · api_server.py', 'VPS',
    'This dashboard. webui.py (:8080) launches seed/reset jobs and serves the app; api_server.py (:8090) holds accounts, sessions and the multi-tenant control plane. Caddy splits the two by path.',
    ['webui.py', 'api_server.py', 'Caddyfile']),
  N('deadman', 'control', 0, 'guard', 'Dead-man switch', 'deadman.py', 'VPS',
    'Destroys the credentials if nobody checks in for too long. In require-check-in mode only a current 2-Step code resets the clock; logins, deploys and SSH sessions deliberately do not.',
    ['deadman.py', 'totp.py'], { page: '/settings', live: true }),
  N('runner', 'control', 1, 'control', 'Job runner', 'webui Job · node_agent', 'VPS',
    'Starts a run as a child process and streams its transcript. The log file on disk, not memory, is the record, which is why a run can outlive the restart that started it.',
    ['webui.py', 'api_server.py', 'main.py'], { page: '/jobs', live: true }),
  N('fleet', 'control', 1, 'control', 'Worker nodes', 'node_agent · fleet_agent', 'Worker node',
    'Extra machines poll the coordinator (nothing ever reaches into them), claim users, and heartbeat their health. A lapsed claim is not handed to another node automatically: resuming depends on the dead node\'s own local ledger.',
    ['node_agent.py', 'fleet_agent.py', 'user_claims.py', 'claim_policy.py', 'join_codes.py', 'export_node_config.py'],
    { page: '/nodes', live: true }),
  N('admit', 'control', 2, 'control', 'Admission & queue', 'job_admission · job_queue', 'VPS',
    'Caps heavy jobs (seed, reset, migrate, setup) box-wide. Past the cap a request waits in line instead of being refused. Admission is advisory for a CLI run: it warns and proceeds.',
    ['job_admission.py', 'job_queue.py'], { page: '/jobs', live: true }),
  N('super', 'control', 2, 'guard', 'Supervisor', 'job_supervisor.py', 'VPS',
    'Notices a run that has stopped making progress and ends it. Every other recovery assumes a process either finishes or dies; a deadlocked one does neither, holding its slot forever.',
    ['job_supervisor.py']),

  // ---- Pacing & resilience ------------------------------------------------
  N('retry', 'pacing', 0, 'control', 'Retry & backoff', 'resilience.py', 'Any machine',
    'Branches on Google\'s error REASON, not the status code: rateLimitExceeded is retried with full jitter; insufficientPermissions and storageQuotaExceeded never succeed and are not. Full jitter, because partial jitter lets colliding threads re-collide.',
    ['resilience.py'], { knobs: ['MAX_RETRIES', 'BASE_BACKOFF', 'MAX_BACKOFF'] }),
  N('quota', 'pacing', 0, 'control', 'Daily upload guard', 'upload_ledger · 750 GB', 'Any machine',
    'Google caps uploads at 750 GB per target user per day; breaching it locks the account out for 24 hours. The count is persisted, so a restart does not forget it. On exhaustion the user is marked PAUSED_QUOTA and the batch continues.',
    ['resilience.py', 'db.py'], { live: true }),
  N('sizing', 'pacing', 0, 'control', 'Worker sizing', 'resources.py', 'Any machine',
    'Divides usable RAM by the per-worker budget and caps the pool at 48 user-workers. Under memory pressure it collapses to one and pauses. On this box RAM, not Google, is what binds first.',
    ['resources.py', 'memtrace.py'], { live: true, knobs: ['USER_WORKERS', 'HARD_CAP'] }),
  N('lim_src', 'pacing', 1, 'control', 'Source limiter', 'AIMD · source project', 'Any machine',
    'A bucket that finds the real ceiling instead of being told it: climbs by probes, backs off multiplicatively (×0.7) at each quota pushback. Against a real quota that is a sawtooth. The source side reads, and is rarely pushed back.',
    ['resilience.py', 'drive_engine.py'], { live: true, knobs: ['DRIVE_PROJECT_QPS_CEILING'] }),
  N('lim_tgt', 'pacing', 1, 'control', 'Target limiter', 'AIMD · target project', 'Any machine',
    'The same controller for writes into the target project, where Google\'s per-project Drive quota is the wall when it binds. Overshoot costs a retry (a 403 rateLimitExceeded is retried and lands); undershoot costs throughput.',
    ['resilience.py', 'drive_engine.py'], { live: true }),

  // ---- Engines ------------------------------------------------------------
  N('pool', 'engines', 0, 'engine', 'Per-user worker pool', 'main.py migrate', 'Any machine',
    'Runs up to N users at once, each worker owning one source/target pair for its whole migration, and each user\'s services in order (drive, gmail, calendar, chat…). Concurrency is across users, not within: every binding quota is per user, so ten threads on ten mailboxes run ~10× one on one.',
    ['main.py', 'phases.py'], { live: true }),
  N('xfer', 'engines', 0, 'engine', 'Transfer mode', 'server_side | download', 'Any machine',
    'server_side never moves bytes through this host: Google copies each file into a staging Shared Drive in the target, and the target user moves it into place (which makes them the owner). download_upload streams every byte through here but works with a strictly read-only source key.',
    ['drive_engine.py', 'native_api.py', 'link_transfer.py', 'ab_transfer.py'], { knobs: ['TRANSFER_MODE'] }),
  N('drive', 'engines', 1, 'engine', 'Drive', 'drive_engine.py', 'Any machine',
    'Folders, files, sharing and (optionally) comments. Idempotent through id_mapping; delta passes compare modifiedTime and update in place, so target IDs and ACLs survive. Files are new objects on the target: Drive cannot move a file across an organisation.',
    ['drive_engine.py', 'native_api.py'], { stage: 'drive', live: true, page: '/scope' }),
  N('perms', 'engines', 1, 'engine', 'Permissions (ACLs)', 'main.py syncacls', 'Any machine',
    'Re-creates share grants, identity-mapped. When a folder is shared the grant is inherited by everything in it, so per-file grants are skipped (~50× fewer calls). A failed folder share gates every file inside it, which is why repair does folders first.',
    ['drive_engine.py', 'main.py'], { stage: 'permissions', live: true }),
  N('shared', 'engines', 1, 'engine', 'Shared Drives', 'shared_drives.py', 'Any machine',
    'Team Drives have their own traversal (corpora=drive) and a membership model instead of per-file ACLs, so they run as a tenant-wide pass rather than once per user.',
    ['shared_drives.py']),
  N('gmail', 'engines', 1, 'engine', 'Gmail', 'gmail_engine.py', 'Any machine',
    'messages.insert, not import: import runs the delivery pipeline (spam, filters, forwarding). Original dates are kept, labels remapped through label_map, drafts included. Google caps sustained writes at 3/sec/account and says that cannot be raised: ~10 minutes per user floor whatever the hardware.',
    ['gmail_engine.py'], { stage: 'gmail', live: true, knobs: ['MIGRATE_GMAIL_SETTINGS'] }),
  N('cal', 'engines', 1, 'engine', 'Calendar', 'calendar_engine.py', 'Any machine',
    'events.import, not insert: insert would send an invitation for every past meeting to everyone who attended it. Keeps iCalUID and organizer. Secondary calendars and sharing rules are opt-in (the latter widens the source grant).',
    ['calendar_engine.py'], { stage: 'calendar', live: true, knobs: ['MIGRATE_SECONDARY_CALENDARS', 'MIGRATE_CALENDAR_ACLS'] }),
  N('contacts', 'engines', 1, 'engine', 'Contacts', 'contacts_engine.py', 'Any machine',
    'Contact groups and entries through the People API.',
    ['contacts_engine.py'], { stage: 'contacts', live: true }),
  N('tasks', 'engines', 1, 'engine', 'Tasks', 'tasks_engine.py', 'Any machine',
    'Task lists and their tasks.',
    ['tasks_engine.py']),
  N('chat', 'engines', 1, 'engine', 'Chat', 'chat_engine.py', 'Any machine',
    'Spaces and messages, replayed as their original senders. Runs last because it is the only pass that can leave a half-built artefact. Needs a Chat app configured in the Cloud console, which has no API.',
    ['chat_engine.py'], { stage: 'chat', live: true }),
  N('groups', 'engines', 1, 'engine', 'Groups', 'groups_engine.py', 'Any machine',
    'The groups themselves and who is in them.',
    ['groups_engine.py']),
  N('links', 'engines', 2, 'engine', 'Link rewrite', 'link_rewrite.py', 'Any machine',
    'Repoints Drive links inside migrated mail at the copies on the target, using the mappings Drive created (which is why Drive runs before Gmail). Handles quoted-printable folding, where a URL is split mid-id across lines.',
    ['link_rewrite.py', 'check_link_rewrite.py']),
  N('dms', 'engines', 2, 'engine', 'Mail via Google DMS', 'dms_migrate.py', 'Browser',
    'An alternative mail leg: hands the mailbox to Google\'s own Data Migration Service, which copies inside Google and so spends none of our Gmail write quota. There is no API for it, so it drives the Admin console; it gives per-user status, not the per-item ledger.',
    ['dms_migrate.py', 'dms_approve.py', 'dms_grant.py', 'dms_read_auth_mail.py'], { page: '/services' }),

  // ---- Ledger -------------------------------------------------------------
  N('id_mapping', 'ledger', 0, 'store', 'id_mapping', 'source id → target id', 'VPS',
    'Every object created, mapped source to target, and consulted before every mutating call: that lookup is what makes a rerun skip what is done. Authoritative; ledger_verify checks it still describes reality.',
    ['db.py', 'ledger_verify.py'], { live: true }),
  N('audit_log', 'ledger', 0, 'store', 'audit_log', 'one row per attempt', 'VPS',
    'Every attempt and its outcome: SUCCESS, FAILED, BLOCKED, SKIPPED_*. Left alone it reached 10.6M rows and 6.1 GB on one tenant, so audit_retention collapses the SUCCESS rows of finished users into counts.',
    ['db.py', 'audit_retention.py'], { live: true }),
  N('upload_ledger', 'ledger', 0, 'store', 'upload_ledger', 'bytes per user per day', 'VPS',
    'Bytes sent to each target user per UTC day. Persisted so the 750 GB/day guard survives a restart.',
    ['db.py']),
  N('label_map', 'ledger', 0, 'store', 'label_map', 'Gmail label ids', 'VPS',
    'Source label id to target label id, so mail lands under the right labels.',
    ['db.py', 'gmail_engine.py']),
  N('run_metrics', 'ledger', 0, 'store', 'run_metrics', 'snapshot every 15 s', 'VPS',
    'The migrating process records latency and request rate per call; a flusher copies a snapshot here every 15 s, because every reader lives in a different process. Bounded to the last hour (240 rows).',
    ['metrics.py', 'db.py'], { live: true }),

  // ---- Target tenant ------------------------------------------------------
  N('staging', 'target', 0, 'tenant', 'Staging Shared Drive', 'server_side only', 'Google',
    'Where server_side copies land before the target user moves them out. If a move fails the copy is left and the drive is never deleted while it holds files: losing bytes is worse than leaving a drive behind.',
    []),
  N('tgt', 'target', 1, 'tenant', 'Target tenant', 'Workspace · write side', 'Google',
    'Where data lands. Accounts must exist, be unsuspended and be licensed: an unlicensed account has no Drive or Gmail and Google says so with errors that never mention licences. Everything written is owned by the target user.',
    []),

  // ---- Verify & repair ----------------------------------------------------
  N('delta', 'verify', 0, 'verify', 'Delta passes', 'main.py delta', 'Any machine',
    'Re-runs drive, gmail and calendar for what changed (modifiedTime, newer_than, updatedMin), nightly between the bulk copy and cutover. Idempotent through id_mapping, so nothing is inserted twice.',
    ['main.py']),
  N('phases', 'verify', 0, 'verify', 'Phase reconcile', 'phases.py', 'Any machine',
    'Runs one service at a time and compares counts taken from the tenants themselves, not the ledger, since the ledger records what the engine believes it did. Stops on a shortfall instead of carrying it forward.',
    ['phases.py']),
  N('verify', 'verify', 0, 'verify', 'Validation', 'verify.py · ledger_verify', 'Any machine',
    'Post-migration reconciliation: the audit log says what the engine believes it did, this checks the target agrees.',
    ['verify.py', 'ledger_verify.py', 'contract_probe.py'], { stage: 'validation', live: true, page: '/verification' }),
  N('acl', 'verify', 0, 'verify', 'ACL audit & repair', 'acl_audit · acl_repair', 'Any machine',
    'Proves file by file that sharing survived (counting is not enough), resolves failures the target says are no longer failures, and re-applies grants that genuinely never landed.',
    ['acl_audit.py', 'acl_reconcile.py', 'acl_repair.py']),
  N('repair', 'verify', 1, 'verify', 'Repair', 'repair.py · retry_failed', 'Any machine',
    'A finished run\'s failure count is not one number. Groups failures into families (one live run had 119,600 from three causes) and fixes the fixable ones. Gmail and Calendar failures are never revisited by a delta pass, so retry_failed does that.',
    ['repair.py', 'retry_failed.py', 'resolve_failures.py', 'repair_modified_times.py'], { page: '/errors' }),
  N('external', 'verify', 1, 'verify', 'External shares', 'external_shares.py', 'Any machine',
    'Who outside both tenants still holds access, and where their files now live. Two different things are true after a migration and conflating them is how people get surprised.',
    ['external_shares.py']),
  N('undo', 'verify', 1, 'guard', 'Undo · reset · wipe', 'undo_migration.py', 'Any machine',
    'Deletes exactly what a migration created (using id_mapping as the record), or empties the target for a rehearsal. Destructive: behind the domain guard and a typed confirmation. wipe_target deletes accounts and freezes their licences for 20 days; reset_target keeps them.',
    ['undo_migration.py', 'reset_target.py', 'wipe_target.py']),
  N('report', 'verify', 2, 'verify', 'Final report', 'report_payload', 'VPS',
    'Item counts from the ledger, duration and throughput, failures by cause, and what was left behind. Counts, never one averaged percentage.',
    ['webui_spa.py'], { stage: 'report', live: true, page: '/report' }),

  // ---- Watch --------------------------------------------------------------
  N('mc', 'watch', 0, 'ui', 'Mission Control', 'live run overview', 'Browser',
    'The run at a glance: stages, users, failures, what to do next.',
    ['migration-webui/src/pages/MissionControl.tsx'], { page: '/mission-control' }),
  N('jobs', 'watch', 0, 'ui', 'Jobs', 'running · stopped · retry', 'Browser',
    'Everything running, every stopped or failed run with a Retry, and the queue.',
    ['migration-webui/src/pages/Jobs.tsx'], { page: '/jobs' }),
  N('perf', 'watch', 0, 'ui', 'Metrics', 'rates · latency · sawtooth', 'Browser',
    'Requests per second, latency by operation, each limiter\'s sawtooth, volume by outcome, and the daily cap.',
    ['migration-webui/src/pages/Metrics.tsx'], { page: '/metrics' }),
  N('activity', 'watch', 0, 'ui', 'Activity', 'recent ledger rows', 'Browser',
    'The newest audit rows as they land.',
    ['migration-webui/src/pages/ActivityFeed.tsx'], { page: '/activity' }),
  N('fail', 'watch', 0, 'ui', 'Failures', 'grouped by cause', 'Browser',
    'Failures grouped by reason with one example each, and the affected users.',
    ['migration-webui/src/pages/ErrorHandling.tsx'], { page: '/errors' }),
  N('verif', 'watch', 0, 'ui', 'Verification', 'source vs target', 'Browser',
    'Per-user comparison of what the source holds and what the target now has.',
    ['migration-webui/src/pages/Verification.tsx'], { page: '/verification' }),
  N('tui', 'watch', 0, 'ui', 'Terminal dashboard', 'tui.py', 'Any machine',
    'The same ledger as a terminal UI, attachable from any session. Its design notes are why progress is reported as counts, never averaged into one percentage.',
    ['tui.py']),
  N('diag', 'watch', 0, 'ui', 'AI diagnostics', 'ai_diagnostics.py', 'VPS',
    '"What is actually going on right now?", answered by a model reading the ledger and the log instead of a person reading both.',
    ['ai_diagnostics.py']),
  N('housekeep', 'watch', 0, 'store', 'Housekeeping', 'backup · retention', 'VPS',
    'Backs up every ledger consistently without stopping anything (there were no backups before), and prunes audit rows of finished users.',
    ['backup_db.py', 'audit_retention.py']),

  // ---- Tooling ------------------------------------------------------------
  N('tests', 'tooling', 0, 'seed', 'Test & measurement', 'suite · probes · benchmarks', 'Any machine',
    'The suite, and the tools that check the tests\' own assumptions: contract_probe compares what the fakes encode against the real APIs, ui_check drives the signed-in UI and asserts it tells the truth, benchmark_run and ab_transfer measure and judge a run.',
    ['test_report.py', 'contract_probe.py', 'ui_check.py', 'benchmark_run.py', 'ab_transfer.py'], { page: '/tests' }),
]

const E = (from: string, to: string, label: string, kind: Kind): PEdge => ({ from, to, label, kind })

const PER_USER = ['drive', 'perms', 'gmail', 'cal', 'contacts', 'tasks', 'chat', 'groups']
const WRITERS = [...PER_USER, 'shared']
const LEDGERED = [...WRITERS]

export const EDGES: PEdge[] = [
  // Setup
  E('wizard', 'gcp', 'Project', 'ctl'), E('gcp', 'keys', 'Service account', 'creds'),
  E('totp', 'wizard', '2-Step code', 'creds'), E('keys', 'dwd', 'Client ID', 'creds'),
  E('wizard', 'dwd', 'Console grant', 'ctl'), E('dwd', 'src', 'Delegation', 'creds'),
  E('dwd', 'tgt', 'Delegation', 'creds'), E('dwd', 'tenantcfg', 'Verified scopes', 'ctl'),
  E('keys', 'tenantcfg', 'Key path', 'creds'), E('tenantcfg', 'scopeguard', 'Tenant pair', 'map'),
  E('tenantcfg', 'domguard', 'Protected domains', 'ctl'),
  // Rehearsal
  E('corpus', 'seed', 'Fabricated data', 'items'), E('seed', 'src', 'Writes', 'items'),
  // Plan
  E('src', 'disc', 'Scan', 'items'), E('src', 'coverage', 'Inventory', 'items'),
  E('disc', 'idmap', 'Directory', 'map'), E('idmap', 'provision', 'Pairs', 'map'),
  E('idmap', 'preflight', 'Pairs', 'map'), E('provision', 'tgt', 'Accounts', 'items'),
  E('coverage', 'report', 'What is left behind', 'ctl'),
  // Control
  E('preflight', 'runner', 'Go / no-go', 'ctl'), E('scopeguard', 'runner', 'Go / no-go', 'ctl'),
  E('idmap', 'runner', 'Pairs', 'map'), E('tenantcfg', 'runner', 'Tenant pair', 'map'),
  E('ui', 'runner', 'Start / stop', 'ctl'), E('ui', 'fleet', 'Directive', 'ctl'),
  E('runner', 'admit', 'Wants a slot', 'ctl'), E('runner', 'super', 'Progress', 'ctl'),
  // Pacing feeds the pool
  E('retry', 'lim_src', 'Pushback', 'ctl'), E('retry', 'lim_tgt', 'Pushback', 'ctl'),
  E('runner', 'pool', 'Users', 'ctl'), E('fleet', 'pool', 'Claimed users', 'ctl'),
  E('sizing', 'pool', 'Workers', 'ctl'), E('lim_src', 'pool', 'Read permit', 'ctl'),
  E('lim_tgt', 'pool', 'Write permit', 'ctl'), E('quota', 'pool', 'Upload budget', 'ctl'),
  E('keys', 'pool', 'Tokens', 'creds'), E('runner', 'shared', 'Tenant-wide pass', 'ctl'),
  E('pool', 'xfer', 'User turn', 'ctl'), E('xfer', 'drive', 'Transfer mode', 'ctl'),
  ...PER_USER.map((id) => E('pool', id, 'User turn', 'ctl')),
  // Reads
  ...[...PER_USER, 'shared'].map((id) => E('src', id, 'Reads', 'items')),
  E('src', 'dms', 'Mailbox', 'items'),
  // Writes
  ...WRITERS.map((id) => E(id, 'tgt', 'Writes', 'items')),
  E('xfer', 'staging', 'Copies', 'items'), E('staging', 'tgt', 'Moved into place', 'items'),
  E('drive', 'links', 'Drive mappings', 'map'), E('gmail', 'links', 'Messages', 'items'),
  E('links', 'tgt', 'Rewritten mail', 'items'), E('dms', 'tgt', 'Mail', 'items'),
  // Ledger
  ...LEDGERED.map((id) => E(id, 'audit_log', 'Rows', 'items')),
  ...LEDGERED.filter((id) => id !== 'perms').map((id) => E(id, 'id_mapping', 'Mappings', 'map')),
  E('gmail', 'label_map', 'Label ids', 'map'), E('quota', 'upload_ledger', 'Bytes sent', 'items'),
  E('pool', 'run_metrics', 'Call timings', 'items'),
  // Verify & repair
  E('tgt', 'verify', 'What is really there', 'items'), E('id_mapping', 'verify', 'Mappings', 'map'),
  E('audit_log', 'verify', 'Attempts', 'items'), E('verify', 'report', 'Result', 'map'),
  E('tgt', 'phases', 'Counts', 'items'), E('src', 'phases', 'Counts', 'items'),
  E('ui', 'phases', 'Run by phase', 'ctl'), E('phases', 'report', 'Shortfalls', 'map'),
  E('src', 'delta', 'Changes', 'items'), E('id_mapping', 'delta', 'Already copied', 'map'),
  E('audit_log', 'delta', 'Modified times', 'items'), E('runner', 'delta', 'Delta pass', 'ctl'),
  E('delta', 'tgt', 'Changed items', 'items'),
  E('tgt', 'acl', 'Grants', 'items'), E('id_mapping', 'acl', 'Mappings', 'map'),
  E('acl', 'report', 'Proof', 'map'), E('acl', 'tgt', 'Re-applied grants', 'items'),
  E('audit_log', 'repair', 'Failures', 'items'), E('repair', 'tgt', 'Fixes', 'items'),
  E('repair', 'report', 'Residue', 'map'),
  E('tgt', 'external', 'Shares', 'items'), E('external', 'report', 'Who still has access', 'map'),
  E('id_mapping', 'undo', 'What was created', 'map'), E('domguard', 'undo', 'Refuses protected', 'ctl'),
  E('undo', 'tgt', 'Deletes', 'items'),
  // Watch
  E('audit_log', 'activity', 'Recent rows', 'items'), E('audit_log', 'fail', 'Failures', 'items'),
  E('audit_log', 'mc', 'Counts', 'items'), E('run_metrics', 'perf', 'Snapshots', 'items'),
  E('verify', 'verif', 'Results', 'map'), E('runner', 'jobs', 'Running', 'ctl'),
  E('admit', 'jobs', 'Queue', 'ctl'), E('fleet', 'jobs', 'Node jobs', 'ctl'),
  E('audit_log', 'tui', 'Counts', 'items'), E('audit_log', 'diag', 'Ledger', 'items'),
  E('run_metrics', 'diag', 'Metrics', 'items'), E('audit_log', 'housekeep', 'Prune & back up', 'ctl'),
]
