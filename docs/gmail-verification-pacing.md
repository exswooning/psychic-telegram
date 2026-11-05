# Gmail verification quota pacing

Gmail batches reduce HTTP overhead; every inner message read still costs quota.
The verifier now shares a weighted mailbox budget across its worker threads and
local verification processes, keyed by credential project and effective delegated
user (including domain-handover aliases). Source and target budgets are independent
when their project/user identities differ.

Defaults reserve 20% of the current 6,000 units/minute/user quota: metadata reads
cost 20 units, raw reads cost 60. These are the current quotas for newer projects;
older projects can retain their previous quota and method costs. Only configure
`VERIFY_GMAIL_UNITS_PER_MINUTE`, `VERIFY_GMAIL_METADATA_UNITS` and
`VERIFY_GMAIL_RAW_UNITS` after confirming that project's actual limits.

On a quota 403 or 429, the shared budget halves its rate and waits at least a
minute (longer if Retry-After requests it). Concurrent rejected batch parts cause
one reduction per cooldown. Recovery is gradual and bounded by the configured
budget. Permission failures remain item findings, not quota signals. Cooperative
stop remains responsive during pacing waits.

Successful batch reads and permanent failures are retained. Retries rebuild only
transiently rejected or unanswered parts; every attempt is charged for the actual
number of messages retried. Single-message fallback reads use the same pacing.

State lives in `~/.cache/bitport-gmail-verification/quota.sqlite3`, private to the
service's OS user. It contains hashed bucket identities, rates and timestamps;
no tokens or message content. Separate hosts or OS users do not share this file,
and other applications/migration engines do not consume this verifier budget.
Headroom and adaptive cooldown cover competing traffic, not a quota guarantee.

This changes verification reads only. It does not rewrite mail, mutate the
source, relax verification criteria or certify outstanding findings as resolved.

Validation covers weighted scheduling, cross-process sharing, effective identity,
cooldown/Retry-After, cancellation, selective retries after partial responses,
permanent permission failures, and single-message fallback pacing.

References: https://developers.google.com/workspace/gmail/api/reference/quota
and https://developers.google.com/workspace/gmail/api/guides/handle-errors.
