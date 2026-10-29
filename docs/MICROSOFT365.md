# Microsoft 365 workspace — implementation status

The Microsoft page is at `/next/microsoft` in the workspace interface (or `/app/microsoft` if that interface is built). Its API prefix is `/api/v2/microsoft`.

## Available in this version

- Separate source/target tenant GUIDs, application GUIDs, Microsoft cloud endpoints, expected geography and source-protection planning mode.
- Host-local certificate import and encrypted private-key storage. No tenant passwords, client secrets or private keys enter the web interface.
- Pinned Graph v1.0 origins, pagination, read retries with Retry-After, cooperative cancellation and no source write client.
- Read-only inventories of directory profiles, domains, organizations/licenses, groups/owners/members, mailbox folders/messages, calendars/events, contacts/folders, To Do lists/tasks, OneDrive hierarchies, file permissions/versions, SharePoint sites/libraries/list definitions, Teams/channel top-level messages, group Planner plans/tasks/buckets, application identities, Intune devices and legacy device configuration collections.
- Per-scope results that distinguish completed empty lists, partial reads, authorization failures, errors, missing adapters and unchecked scopes.
- Explicit mapping of existing users by immutable IDs; local-part matches are suggestions only. Exact Unicode name/profile comparisons are available.
- Two target-only **sandbox pilots**: supported profile field patches, and current OneDrive file content/folder copies with full byte SHA-256 target readback.
- Durable copy intents, receipts, unresolved repair findings, report download, safe stops and explicit metadata recovery for exited workers.
- Existing cross-account resource admission, including serialization with other jobs for the same account.

Certificate presence does not establish API consent. A returned API collection does not establish tenant-wide business coverage. Examples: mailbox Graph listing is not archive/recoverable-item completeness; SharePoint list definitions are not list items/pages/workflows; top-level channel messages are not complete Teams history. These gaps remain explicit.

## Not implemented or not qualified

This is **not yet a complete Microsoft tenant migration engine**. Production transfer routes are unavailable. Native Migration Orchestrator, Exchange move configuration, native OneDrive/SharePoint moves, mailbox opaque-stream copy, Teams imports, permission replay, versions/history transfer, identity creation, application reconstruction, supported background deltas, domain cutover and automated repair execution are not implemented here.

OneDrive sandbox copies preserve the current file bytes and folder hierarchy under a separate destination folder. They do not preserve versions, original timestamps, sharing, native IDs or application behaviour. Shortcuts and special/remote objects are findings. The overall pilot remains NEEDS_ATTENTION even when its individual file byte checks pass. A profile VERIFIED result covers only the selected supported fields.

No Microsoft tenant has been used to qualify this code. Mock tests cannot release a production migration capability. The catalogue deliberately keeps `transferQualified=false` and the API keeps `productionReady=false`. The existing OneDrive placeholder in `providers.py` remains unimplemented; the Google engines do not call these adapters.

## Host setup

Use the Bitport service's Python environment:

```sh
python -m pip install -r requirements-microsoft.txt
```

Register certificate-backed Entra applications in both tenants. Give the source read permissions only. Administrator consent must be performed by an authorized administrator. Confirm commercial/government/China cloud identity and endpoint compatibility.

For each side, import a matching PEM private key and public certificate on the host as the Bitport service user:

```sh
python microsoft365_cli.py install-certificate \
  --account-id ACCOUNT_ID --side source \
  --tenant-id TENANT_GUID --client-id APPLICATION_GUID \
  --private-key /secure/source.key --certificate /secure/source.crt
```

Repeat with `--side target` and its own values. Never put private-key contents or tokens on the command line. The CLI rejects a mismatched key/certificate and expired certificates. Certificate renewal uses the same import command, preserving the configured tenant/application identity.

Then save both connections on the Microsoft page and explicitly start discovery. This does not alter the Google tenant configuration associated with the selected Bitport account.

### Read permissions

Start with the least privileges for the inventories required by the project. The page lists permission names per workload; the current routes use application authentication. Some clouds/resource types might not support a proposed permission or collection; those must remain failed/unsupported scopes until qualified.

Common read permissions include `Organization.Read.All`, `Domain.Read.All`, `User.Read.All`, `Group.Read.All`, `Files.Read.All`, `Sites.Read.All`, `Mail.Read`, `Calendars.Read`, `Contacts.Read`, and workload-specific Teams/Planner/Intune permissions. Validate current Microsoft documentation and resource-specific application restrictions before consent. Do not grant broad write scopes merely to make a failed discovery look successful.

Mailbox access can be restricted by Exchange application access/RBAC. Site-specific permissions can also produce partial inventories. Check both enumeration and per-resource reads; possession of a token does not prove broad access.

### Sandbox write pilots

These remain off by default. After establishing that both tenants are disposable sandboxes, an operator may enable this environment setting on the host:

```text
BITPORT_MICROSOFT_SANDBOX_WRITES=1
```

The saved project must also be marked sandbox. Confirm an existing source/target user mapping. The UI requires a typed operation-specific approval and binds it to the current plan hash. Additional target permissions are needed: the current supported user update permission for profile patches, and the current upload-session application permissions for file copies. Source credentials remain read-only.

The code does not create missing users, assign licenses or provision missing target OneDrives. Synchronized target profiles are blocked and must be changed at their authoritative directory. A missing target site is a blocker, not permission to provision one accidentally before native-method selection.

## Storage and interruption behaviour

Runtime files live in `data/microsoft365/<account-id>/`, separately from every Google ledger. The directory is ignored by Git. It contains a SQLite WAL database, encrypted certificate files and a private vault key. Back up the vault key together with encrypted credentials securely; losing it makes those credentials unreadable. File copies use a temporary staged file and keep at least one GiB of disk headroom. Large files that exceed staging capacity are blocked.

Temporary Graph download/upload/import URLs are not persisted in inventory objects. Continuation links are followed only on the configured Graph v1.0 origin. Storage transfers do not forward the Graph bearer token to a download/upload URL. Raw HTTP exception text is not logged because it can contain authenticated URLs.

The initial copy is sequential and conservative; adaptive worker scaling is not implemented. Item-level progress is durable. Successful files are re-read and compared when a pilot is retried; existing untracked names are not overwritten. An upload interrupted before final commit may restart at the item boundary. This is not persistent byte-offset resume across worker restarts.

Stop requests take effect between requests/chunks or during retry waits. A blocked network request can take its configured timeout to return. After an unclean exit, use “Reconcile exited worker”; it requires old activity and confirmation that the process is gone. It only updates job metadata. It never starts or kills a process. Unresolved intents stay visible for review.

## Qualification and next build gates

1. Obtain two authorized disposable Microsoft tenants and certificate applications; confirm cloud and licensing.
2. Exercise actual read permissions and pagination against seeded data, including failures and inaccessible objects.
3. Qualify profile and OneDrive content pilots: source invariance, interruption/retry, no duplicate items, parent structure, target readback and disk constraints.
4. Implement and qualify native user-data migration plus the required target MailUser preparation. Source-changing moves need separate approval and cannot execute under preserve-source mode.
5. Implement remaining file metadata, versions, ACLs, mail copy, collaboration and application adapters, with workload-specific evidence.
6. Add method-specific deltas, resource priority, geography evidence and approved domain cutover/final catch-up.
7. Enable a production capability only after its supported feature set and exceptions are backed by a reviewed live qualification report.

No production migration, wipe, domain switch or external permission change is performed by installing this version.
