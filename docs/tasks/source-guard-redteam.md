# Source write guard red team — 27 November 2026

## Harness

`tests/test_source_write_attacks.py` builds production `AuthManager` clients over
a fake transport that records method, URL, headers, and body. It exercises all
831 non-GET/HEAD discovery methods through the source client routes for every
API in `auth._API_VERSIONS`, plus Directory and Cloud Identity. It also tests
batches, the source service's raw `._http`, a mutated `HttpRequest`, uploads,
the staging copy/move exceptions, and government-domain write-client guards.

All discovery cases are plain regression tests now; there are no xfail
markers. The bare `auth._http()` factory is excluded because it has no
credentials and is not a source client. The source service's authenticated
`._http` path remains covered.

## Discovery matrix outcomes

| Route | API/methods | Outcome after the transport guard | Why |
|---|---|---|---|
| `api("source", api, user)` | Drive 36; Gmail 49; Calendar 26; Chat 24; People 13; Tasks 10; Sheets 13; Docs 2; Slides 2; Forms 6 | All writes refused except the three read-only POST methods below | Source Drive is wrapped and source API transports enforce `check_source_request`. |
| `_service("source", api, user)` | Same counts | Same outcome as `api(...)` | The source client construction path installs the same guard. |
| `source_drive()` | Drive 36 | All locally refused | `ReadOnlyDrive` refuses collection writes; the source transport checks requests after construction too. |
| `allow_copy_into(source_drive(), staging, moves=False)` | Drive 36 | One staging copy allowed; 35 refused | Only `files.copy` whose parent is the active staging drive is permitted. |
| `allow_copy_into(source_drive(), staging, moves=True)` | Drive 36 | One staging copy allowed; 35 refused | A complete move update is a separate positive control; the generic update attacks are refused. |
| `source_directory()` | Admin Directory 82 | Writes refused before recorder | The source transport and AuthorizedHttp hook guard the admin client. |
| `directory("source", writable=False)` | Admin Directory 82 | Writes refused before recorder | Read-only client construction still installs the source transport guard. |
| `directory("source", writable=True)` | Admin Directory 82 | Refused locally for `dharan.gov.np` | `domain_guard` rejects a government source before constructing the client. |
| `directory("source", groups=True)` | Admin Directory 82 | Refused locally for `dharan.gov.np` | The group-write route is covered by the same domain guard. |
| `cloud_identity("source")` | Cloud Identity 33 | Writes refused before recorder | The source transport and AuthorizedHttp hook guard this client. |

The matrix has 831 cases: 823 are refused, six are the read-only POSTs below,
and two are the permitted staging copies. The 823 include both source
Directory write-scope routes rejected at construction.

## Read POSTs treated as reads

These methods use POST but are read-only queries, so the transport policy
allows them and the recorder classifies them as non-leaks:

- Calendar `freeBusy.query` — once each through `api(...)` and `_service(...)`.
- Sheets `spreadsheets.getByDataFilter` — once through each route.
- Sheets `spreadsheets.values.batchGetByDataFilter` — once through each route.

The probe uses `auth._READ_POSTS`, matching the transport guard's allowlist.

## Transport, upload, and move outcomes

| Attack | Outcome | Why |
|---|---|---|
| `source_drive().new_batch_http_request()` with GET-only contents | Allowed | The batch envelope uses POST, but every nested request is a read. |
| `api("source", "drive", user).new_batch_http_request()` with GET-only contents | Allowed | Same read-only batch policy on the source API route. |
| Either batch route with DELETE | Refused before transport | Source batches may contain reads only. |
| Source service `._http.request(...)` | Refused before recorder | The authenticated source transport is guarded even when called directly. |
| Read `HttpRequest` changed to PATCH after creation | Refused at execute | The transport rechecks the final method, URI, and body. |
| Multipart and resumable MediaUpload through the Drive source facade | Refused at request construction | `ReadOnlyDrive.files().create` raises `SourceWriteRefused`; no request exists or leaves. |
| `files.copy` into the active staging drive | Allowed | This is the migration's designated source-side copy. |
| Complete move update into staging | Allowed | Requires `addParents` to staging, nonempty `removeParents`, no body/media, and only the permitted query fields. |
| Move update with `addParents` but no `removeParents` | Refused | It would leave the source parent in place; this was the harness's narrower move finding. |
| Copy to another destination or move with extra metadata | Refused | The staging exceptions do not authorize other source changes. |

## CLI behavior

`source_attack_probe.py --dry-run --domain a.sarafgloabalexim.com --user <user>`
builds and prints the real-client matrix without executing generated API
methods. Normal AuthManager credential setup may still refresh or probe OAuth
tokens.

Live mode requires `--live --domain a.sarafgloabalexim.com --user <user> --file
<file-id> --i-understand`; it refuses other domains and users outside the
sandbox. Each generated write request is changed before execution into a
description PATCH on that one file. The CLI uses its current ETag, restores the
original description and modified time if the probe gets through, and verifies
the restoration. If the probe marker no longer matches, it stops cleanup rather
than overwriting another edit. Output distinguishes local refusal, Google 403
scope/permission refusal, and `WENT THROUGH`.

This session did not run credential-backed dry-run or live mode. The harness
ran only with its fake transport; Claude owns the live run.

## Verification

- `.venv/bin/python -m pytest --runxfail -o addopts=--tb=short tests/test_source_write_attacks.py -q` — 847 passed in 5.40s.
- `.venv/bin/python -m pytest -o addopts=--tb=short tests/test_source_write_attacks.py tests/test_source_transport_guard.py -q` — 878 passed in 6.92s.

