# Fresh account-3 rehearsal

Operator request: wipe target data, run a full migration, manage recoverable errors,
schedule quota resumes, then deep-check and fix remaining parity blockers.

Scope: 45 current source/target user mappings, s.sarafgloabalexim.com to
t.sarafgloabalexim.com; Chat excluded. Keep accounts, credentials, delegation and
domain protection. Drive uses server-side copy; changed link mail precedes a fresh
DMS import. Source reads only; source Calendar ACL migration disabled.

`sandbox_campaign.py --directory <private campaign directory> --start` admits ONE
fresh wipe. Subsequent timer calls omit `--start`. The durable phase marker is
written before destructive work. Interruption, partial wipe, mapping/code drift,
unavailable authorization, non-quota copy failures or unknown DMS state hold for
review. Never clear the marker to repeat a wipe. Before deletion, all five target
service grants and group grant, actual DMS idleness, all-account process/repair
idleness and a complete 45-user source baseline must be established. A consistent
SQLite backup preserves old findings, receipts and quota usage.

Reset only successfully wiped service/user evidence, including receipts, cache,
reconciliation and queued findings. Other users/services and real upload quota
remain. Delete owned Drive Trash items explicitly; never blanket-empty foreign
Trash. List task/calendar/group pages before mutation.

Normal deployment gates are strengthened to recognize the campaign itself; no
force/over-jobs override. The installed Python manifest is pinned at campaign
start and checked on each phase. Autopilot can use isolated state and observe-only
mode so it cannot race the campaign's durable recovery. Typed target repairs run
only after copying, through the existing admitted repair implementations.

Copy retries: at most four attempts; only PAUSED_QUOTA gets an automatic delayed
resume. Wait until the affected users' recorded upload buckets have expired
(bucket start +25h, conservative), or 24h from observed refusal if unknown. Do not
interpret minute-level API throttling as a daily wait. DMS is started fresh once;
an interrupted or failed DMS phase requires actual status/error review, not another
fresh import. A server systemd timer provides persistence without overlapping
phases or an agent being online.

Post-copy: full six-service user checks, supported typed target repairs/rechecks,
whole-tenant read-only check, offline deep ledger audit, source-after guard and
strict readiness evidence audit. Retain findings and stop in REVIEW_REQUIRED;
never equate process completion with parity. Unsupported features, source changes,
provider access failures and remaining differences need explicit investigation.
Generated code still requires review, meaningful tests, publication and idle-gated
deployment; this campaign does not automatically deploy model suggestions.
