# Bitport: automatic migration assurance and engineering escalation

Status: proposed architecture, 29 October 2026. The full incident/agent system described here is not installed or enabled. The current setup-notice work is one small prerequisite.

## Outcome and honest completion rules

Operators should launch a migration once. The same durable run should handle ordinary retries, verification, approved repairs, and final catch-up without someone starting separate repair and verification jobs.

The quality target is zero unresolved findings across the declared, supported migration scope. A migration may show **Verified** only after independent evidence meets that contract. **Blocked**, **Needs approval**, **Completed with exceptions**, and **Unsupported** remain distinct outcomes. Unknown or unreadable data is never treated as empty or matched.

“No errors on every first attempt” is an engineering objective, not a guarantee. Providers can reject attachments, deny external-owner access, change APIs, require MFA or new terms, impose licences, or omit migration APIs for a feature. Bitport must expose these facts and route them to a responsible operator. An AI agent cannot manufacture missing source data or rights that the customer does not possess.

Report three separate measures: first-attempt success; success after automatic recovery in the original run; and success requiring engineering or a human action. Do not make first-attempt statistics look better by hiding retries.

## End-to-end workflow

Enrollment and preflight → source inventory and capability contract → migrate and validate each item → repair discrepancies within the run → independent completion audit → final delta and audit → evidence report.

Any unresolved incident enters: classify → known safe recovery → reproduce → coding agent → independent review and qualification → canary → idle release gate → resume affected work → independent recheck → close incident.

### 1. Enrollment for several operators

- Tenant and migration access is explicit. Operators see and operate only assigned migrations; viewers cannot launch jobs or approve access.
- Source domains are protected by default. Each transfer method declares its source and target mutations; admission refuses a method that conflicts with the protection policy.
- Operators approve the initial supported service scope, identity mappings, allowed repair operations, AI evidence policy, and spending limits.
- Legal agreements, MFA, new grants, external-owner requests, licensing and billing decisions are directed to the assigned customer operator. The system should not require the platform owner's involvement in routine operations.
- No cross-tenant code, credentials, ledgers, screenshots or private content are mixed in reports or agent sessions.

### 2. An explicit migration contract

Before work begins, save a versioned contract naming the source and target, mapped identities, included services, exclusions, fidelity requirements, chosen methods, allowed fallbacks, source-protection policy and provider limitations.

Inventory every page of the included source data and record coverage. Include externally owned items and shared drives where included; do not certify an owned-files-only inventory as all Drive data. Store inventory watermarks and stable item IDs. Reconcile source changes using a new recorded watermark, not by quietly rewriting expected results to match the target.

Service contracts should include:

| Service | Required evidence |
|---|---|
| Drive | Source-ID coverage; target existence; file type; binary hashes; native-file feature checks; folder-parent graph; shortcut targets; ownership and mapped effective sharing; link rewriting; explicit revision/comment limitations |
| Gmail | All source IDs including drafts where in scope; content/attachments; labels; dates and mailbox state; missing-Message-ID handling; duplicate prevention; rejected attachments identified |
| Calendar | Calendars, events, recurrence and exceptions, time zones, attachments, reminders, mapped ACLs and supported attendee/organizer behavior |
| Contacts/tasks | Required fields, groups/list relationships, photos or attachments where supported, hierarchy, dates, completion state and ID coverage |
| Groups/profiles | Names, descriptions, aliases, member roles/nested groups, supported group settings, user profile fields, and clearly separate group archive limitations |
| Shared drives/Chat/other services | Full advertised inventory and supported content, membership and hierarchy checks; unsupported features remain visible |
| Storage configuration | Exact reported byte strings and timestamps plus separate evidence for user/OU/group limits, inheritance, shared-drive limits and licensing; no claim that quota usage is an editable policy |

For Google-native files, a matching text export cannot establish complete feature fidelity. A disconnected data source or unavailable API must produce a named coverage gap.

### 3. Verification inside the original migration

Maintain an item state machine: discovered → planned → copying → target observed → validated → committed. A ledger entry alone does not prove that a write landed. A successful write response must be followed by target observation and the relevant comparison before an item is considered validated.

Each item has an idempotency key and durable source/target mapping. Interrupted writes use duplicate detection before retrying. Missing Message-ID mail needs canonical content and attachment fingerprints plus unambiguous target evidence; ambiguous candidates are not guessed.

Findings enter a durable repair queue immediately. Repairs use the same admission limits and source protections, capture before/after evidence and are followed by fresh checks. An independent verifier must inventory both sides and detect items omitted by the migration engine itself. It should share schemas with the engine, but must not rely solely on the engine's success flags or selected sample.

Storage gaps are an additional diagnostic: distinguish owned-file charging, externally owned copies becoming target-owned, trash, revisions, pooled capacity, reporting delay and actual missing data. Counts or equal GB values are not a fidelity certificate.

### 4. Durable automatic recovery

Use the existing account-scoped queue/admission system as the starting point; add durable leases, heartbeats, resumable checkpoints and a transactional event outbox. A laptop reboot, agent outage or browser closing must not lose the repair or incident record.

| Incident class | Automatic response |
|---|---|
| Rate limit/transient service or network error | Bounded exponential backoff with jitter, shared quota pacing and a deadline |
| Expired access token | Refresh existing authorized credentials; do not expand scopes |
| Target propagation delay | Bounded read-after-write retry; retain original evidence |
| Lost mapping | Recover only from verified, unambiguous destination evidence |
| Missing copied item | Duplicate-safe target recovery and independent recheck |
| Capability/permission/owner/licence gap | Preserve evidence; route the exact required action to the responsible operator |
| Unexpected Google UI | Classify the visible screen; offer an audited account/run-specific review or on-device handoff |
| Source mutation, wrong-account write or duplicate explosion | Stop affected writes immediately; isolate the incident and preserve evidence |
| New reproducible software bug | Escalate to the engineering agent queue |

Bound retries per item, error fingerprint and run. Repeated failure opens a circuit breaker; it does not produce an endless loop or an escalating API bill. Recovery should yield to active migration and verification work when resource pressure is high.

### 5. Measure correctness, speed, resources and cost

Every measurement must identify run, tenant pair, service, item type, actual transfer method, source revision, code commit, contract version and timestamps. Separate planned method from the method actually used and record every fallback.

Collect:

- Full source-ID coverage, matched/failed/unread/unsupported items, content and feature differences, folder graph violations, ACL differences, duplicate candidates and final-delta lag.
- Discovered/copied/validated item and byte counts; p50/p95/p99 latency; end-to-end elapsed time; time in copy, validation, retry, queue and repair; raw and effective throughput.
- API requests, status/error categories, per-project/per-user quota utilization, throttle time and backoff; no credential-bearing URLs or tokens.
- CPU, memory, disk capacity/temp-disk peaks, I/O and network bytes; worker count and scaling reasons; process/queue heartbeat age and last successful item/API activity.
- AI calls, tokens and spend; repeated-incident rate; safe-repair effectiveness; patch canary/rollback rate; time to verified recovery.

Use counters continuously and aggregated histograms rather than expensive per-item dashboards. Collect resource measurements at a modest interval, retain detailed traces around failures, and compact old successful item evidence according to retention policy. Exhaustive correctness checks and low-overhead observability are different workloads and need separate budgets.

Benchmark each transfer method by representative file type and size using disposable synthetic fixtures. Compare cold runs separately from cached/skipped runs. Keep source/target fixtures and validation requirements equivalent. Include metadata, permissions, verification and staging time in end-to-end comparisons. Production customer runs are observation data, not destructive benchmark fixtures.

Inject failures in qualification: interrupted copy before mapping commit, duplicate response, 401/403/404/429/5xx, lost network, full disk, worker death, reboot, changed source, expired handoff and provider UI changes. A method is promoted by both correctness and speed evidence, not a single fast aggregate.

### 6. Automatic escalation to Codex and Claude

Run a persistent **Bitport engineering supervisor** with provider adapters. This is a backend integration, not a promise that this desktop chat will stay awake indefinitely.

1. Deduplicate incidents by error category, stack/function, service, file type and code version. Do not send one AI request per failed file.
2. Try a versioned, deterministic repair rule first.
3. For an unknown reproducible incident, build a redacted evidence bundle: contract/coverage, relevant event timeline, error/stack, code version, sanitized minimal fixture and expected invariant.
4. Start or resume a durable Codex or Claude agent session. Use one primary agent; escalate to the other for independent review, a failed attempt or unavailable provider. Prevent both agents from editing/deploying the same incident simultaneously.
5. Give the coding worker an isolated checkout, synthetic data and constrained tools. Keep production credentials, tenant DBs and arbitrary production shell access out of the coding environment.
6. Require the agent to reproduce the failure and add a regression check before changing code. Produce a patch and machine-readable report.
7. Have the other provider review the patch. Deterministic tests and invariant checks remain the deciding gate; agreement between models is not proof.
8. Qualify and release only through the controlled path below. A diagnostic explanation or passing unit test does not close a customer incident.

The provider is configurable, with explicit time, attempt and spend budgets. If a provider is unavailable or a budget is reached, retain the incident and show its status; never discard it or mark the migration Verified.

No customer mail bodies, documents, passwords, tokens or raw screenshots go to either AI provider by default. Any case needing private content uses a customer-approved data policy and a minimal, redacted sample. Do not post customer evidence into public GitHub issues or commits.

Official integration options: [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) supports programmatic start/continue/resume of local agents; [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) embeds an agent with sessions, hooks and permission controls. Managed hosting is optional; [OpenAI Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview) is separately metered. Product authentication, API eligibility and budget must be configured once; do not assume desktop subscriptions provide unlimited embedded commercial agent access. Anthropic's SDK documentation explicitly directs third-party products to API-key authentication unless otherwise approved.

### 7. Controlled unattended release

Local versioned source → private branch/PR and reproducible regression → tests and independent review → sandbox canary → signed artifact and hash manifest → admission/idle gate → deploy → independent service/data check → resume original run.

Start in shadow mode: agents propose and qualify patches while ordinary deterministic repairs can run automatically. Promote a bounded class of fixes to unattended release only after qualification evidence supports it. Configuration changes, permission expansion, source writes and destructive actions are not authorized just because an agent proposes them.

An automatic patch must not weaken source protection, tenant isolation, approval gates or fidelity checks; silence findings; change expected source inventories to hide a difference; or print/export credentials. Such changes stop at a policy gate.

Drain workers or use the existing global idle deployment gate; never force-restart active tenant jobs. Keep rollback artifacts and schema compatibility. A failed canary rolls back code and pauses the affected method. Data deletion is never an automatic rollback technique. Avoid repeated schema changes for an incident fix unless a qualified migration and recovery path exist.

An incident closes only after the affected real items are independently rechecked and the defect's regression is retained. Other affected migrations receive the qualified correction without sharing customer evidence.

### 8. Operator experience

One migration page shows: coverage contract, data/feature differences, actual method, throughput/ETA, source protection, storage-gap explanations, retries, automatic repairs and current incident/agent status.

An incident timeline shows: detected → safe recovery attempted → reproduced → agent assigned → patch qualified → canary → deployed → rechecked. Each transition includes evidence, owner and version. “Waiting on Google”, “Needs your approval”, “External owner action”, “Budget paused” and “Engineering investigating” have explicit meanings and next actions.

The platform owner receives a summary and only exceptional platform-level alerts. Customer operators handle their assigned authentication/access decisions. Successful routine recoveries need no owner notification. There is an emergency stop for writes and a per-tenant kill switch for agent-assisted changes.

### 9. Hosting and resource policy

The laptop can host the controller and durable queue initially. Use supervised services, automatic restart, encrypted credential storage, backup/restore checks and an always-on network/power configuration. Work resumes from checkpoints after downtime. A single laptop is still a single point of failure; no agent can run there while it is offline. Background services must use a separately approved machine identity when they need to reach a protected API; they must not depend on a browser email-code session staying signed in or bypass Cloudflare Access when it expires.

Allocate migration work before engineering work. Run at most a small, measured number of coding/qualification workers, cap their CPU/RAM/temp disk and yield them under pressure. Worker scaling obeys project quota, per-user quota, available RAM/disk/network and CPU/thermal pressure. Usable filesystem space matters more than the advertised disk capacity.

Avoid a new heavyweight telemetry/database stack at the start. Extend the current queue with leases and an outbox; use local bounded metrics and retention. Move shared control state to PostgreSQL if concurrent controllers and sustained write volume require it. Model/API usage has a budget even when no VPS is rented.

## Implementation sequence and exit criteria

| Phase | Deliverable | Exit criterion |
|---|---|---|
| 1 — Truthful run evidence | Coverage contract, complete inventory, per-item invariants, actual method and source guard | Deliberately omitted/unread items cannot produce Verified |
| 2 — Original-run recovery | Durable repair queue, leases, retry budgets and independent recheck | Crash/interruption tests resume without duplicates or source writes |
| 3 — Benchmarks and diagnostics | MIME/size method profiles, histograms, resource/quota and storage explanations | Comparable cold/warm results and every fallback visible |
| 4 — Engineering escalation | Redacted incidents, provider adapters, isolated reproductions and audited sessions | Synthetic bugs automatically become reproducible patches without customer secrets |
| 5 — Qualification and release | Independent reviewer, regression matrix, synthetic canary, idle deploy and rollback | Bad patches and weakened fidelity checks are refused; good patches recheck affected items |
| 6 — Operated pilot | Several assigned operators and migrations, failure injection, recovery drills | Completion reports are honest, isolation holds, budgets hold, and recurring defects become regression cases |

Begin by establishing the baseline from existing runs. Publish first-attempt success, automatic recovery success, unresolved/unsupported coverage, engineering recurrence and total migration time. Choose improvement targets from those measured values; do not invent a 100% reliability claim before the pilot.

## One-time prerequisites

Tenant/operator assignments; initial service grants and legal approvals; a disposable qualification pair; GitHub/CI access; selected AI-provider authentication and spend caps; customer evidence/retention policy; signing/rollback and backup ownership. These are setup requirements for the proposed service, not requests to send passwords or API keys in chat.
