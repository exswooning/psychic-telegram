# Google Workspace configuration checks in the new UI

The **Other services** page uses the global migration selection for its actions
and evidence. It no longer offers a second account selector that can disagree
with the migration header. Unknown account context prevents actions rendering.

## Read and compare

**Inspect groups and storage** is an explicit, admitted job. Opening or polling
the page only reads a saved report; it does not repeatedly scan Google.

The report includes:

- Both group inventories, read failures and timestamps.
- Group names/descriptions, editable aliases, explicit user/nested-group mappings
  and membership roles. Unmapped external members are reported, not invented.
- Supported Groups Settings fields with source/target values. The optional
  `apps.groups.settings` grant is isolated from baseline migration credentials;
  missing access does not prevent other migration APIs from working.
- Google's original storage byte strings for each mapped user pair, read times,
  Drive/trash differences and reported limits. Unknown is not zero. Pooled
  capacity is not treated as a per-user storage policy.

Source Groups Settings clients expose GET only because that API does not offer
a separate read-only scope. Source Directory reads never request a write scope.
Target reads can fall back to an already-delegated full scope if its readonly
sibling was not separately authorized. This never changes delegation.

## Reviewed target group changes

Applying configuration requires a reviewed report hash and a typed confirmation.
The job re-reads both tenants and refuses a changed or incomplete plan before
making writes. All group objects are created before nested membership work;
existing group metadata and member roles can be corrected. Alias and supported
settings changes are then applied on the target and independently re-read.

There is no delete operation in this feature. Target-only groups, aliases and
memberships are retained and reported. Extra memberships and unmapped members
require review before applying. Source-domain guards run before write clients.
Successful API responses alone do not mark differences resolved.

**Limits:** This does not migrate group archive messages, non-editable domain
aliases or every Google Groups capability. It is not a full migration fidelity
certificate. Workspace user/shared-drive storage policy limits, inheritance,
configuration-group priority and licence capacity still require Admin Console
review; Drive's `storageQuota` GET response cannot apply those policies.

## Setup and release visibility

- A successful dry run is labelled as a preview and cannot unlock target user
  provisioning. A real target key and passing readiness checks are required.
- Unrecognized Google sign-in pages fall back to the existing on-device sign-in
  panel after one minute instead of waiting the full timeout without context.
- Bounded Google screen messages reach the authenticated UI log, with URLs,
  long tokens, terminal auth codes and long numeric codes redacted.
- An expired browser sign-in returns a failure immediately instead of consuming
  a second timeout waiting for the same process.
- Automatic setup no longer accepts Google Cloud legal terms. If required,
  Google's provisioning error directs the operator to review them themselves.
- System Health can show backend/new-UI manifest verification and the original
  UI fingerprint. Missing manifests are explicitly unverified.
- Deployment's existing busy gate also recognizes configuration check/apply jobs.

## Qualification and current sandbox scope

Local tests use fake APIs and synthetic UI data. They cover stale plans, missing
permissions, role/alias/settings drift, exact int64 bytes, source write refusal,
existing target data preservation, release integrity and preview provisioning.
These tests do not establish live Google API parity.

The current requested live test is `info@s.sarafgloabalexim.com` to
`nest@t.sarafgloabalexim.com`. Existing target data must be preserved. Missing
mapped target users may be added; no target wipe or user deletion is authorized.
The target's initial server sign-in timed out and was stopped through the UI.
No migration or target provisioning has run in this test yet. Operational checks
and migration controls must continue through `/next/`.
