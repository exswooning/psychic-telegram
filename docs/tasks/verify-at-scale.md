# Verification at scale — 27 Nov 2026

All after measurements below come from the offline fake workloads in
`benchmarks/verify_at_scale_test.py`. Peak memory is Python `tracemalloc`; wall
time is local, not a prediction of Google API latency. Sheets and Drive calls
are represented by fakes. Gmail payload byte totals use the fake's fixed
per-message sizes, while logical request counts reflect both mailbox reads.

| Workload | Before | After | Mismatch coverage |
| --- | --- | --- | --- |
| Dense 10M-cell Sheet | Operator observed about 2,000 paced reads and more than an hour per file. | 146 reads across source and target; 12.001 s; 4.57 MiB peak. The fake compared all 10M cells on each side. | A different cell in a large adaptive window is reported; equal source links are still rewritten before comparison. |
| Empty 20,000 × 120 Sheet | Operator observed 480 paced reads and about 17 minutes. | 6 reads across source and target; 0.002 s; 0.02 MiB peak. | The window walk covers the full allocated extent; blank ranges expand only after an empty response. |
| 2,000-page Doc with 500 images per copy | A large live Doc timed out under the 90-second bound. | 2 Docs API reads, 1,000 image hashes, maximum 8 simultaneous image fetches; 0.453 s; 15.85 MiB peak. | A changed image digest makes the native comparison differ. |
| 200 MiB binary with `VERIFY_TRUST_MD5=0` | Files above 25 MiB were not opened, so this setting did not compare a 200 MiB body. Files under the limit were held whole on both sides. | 400 one-MiB chunks; 0.828 s; 2.00 MiB peak; the planted byte difference was found. | A single changed byte changes the stream hash and verdict. |
| 100,000-message mailbox without receipts | Operator observed 39,756 messages taking 8.7–11.5 hours before the current receipts/read-ahead work. | 200,000 logical message reads; 819,200,000 simulated payload bytes; 1.235 s; 24.62 MiB peak. | Existing raw-message comparison tests cover body differences. |
| 100,000-message mailbox with receipts | Without receipts, full message bodies are fetched again. | 200,000 logical metadata reads; 51,200,000 simulated payload bytes; 0.833 s; 21.94 MiB peak. | Existing receipt tests change target labels and confirm the changed state is reported. |

## Changes

- Sheet reads now fetch matching source and target ranges concurrently. The
  initial range is a small probe; later window height adapts to observed cell
  payloads, with an 8 MiB estimated JSON budget under the Sheets API's 10 MB
  response limit. Provider response-limit errors halve the range and retry.
  Empty ranges expand to the remaining allocated tail. The default 10M-cell
  refusal is gone; an explicitly supplied `max_cells` still fails closed.
- Dense windows are compared as ordered sparse cell iterators rather than
  million-entry dictionaries. Equal grid payloads with no link markers use a
  raw equality fast path. Any Drive or `IMPORTRANGE` candidate still goes through
  the link rewrite before equality can be concluded.
- Binary bodies are downloaded through one-MiB chunks and reduced to MD5,
  SHA-256 and size as they arrive. The two complete files are never retained.
  `VERIFY_TRUST_MD5=0` now forces this content comparison for large files too.
- Docs and Slides source/target reads overlap. Both sides' distinct image URLs
  share one worker pool capped at eight, and production image reads stream into
  SHA-256 while preserving the image size and per-file deadline checks.
- Gmail's existing receipts still replace raw-body reads with metadata reads;
  `VERIFY_GMAIL_WINDOW` bounds read-ahead. The fake shows the request count stays
  the same while represented response bytes and peak allocations fall.

The performance harness is `benchmarks/verify_at_scale_test.py`. The focused
regression command passed:

```text
.venv/bin/python -m pytest tests/test_sheet_window_growth.py tests/test_native_images_compared.py tests/test_image_fetch_deadline.py tests/test_verify_streamed_binary.py tests/test_verify_sample.py tests/test_verify_unverified.py tests/test_verify_content_memory.py -q
PASS (no failures)
```

The harness also passed:

```text
.venv/bin/python -m pytest benchmarks/verify_at_scale_test.py -q -s
1 passed; all five workload groups completed
```

An earlier version of the harness spent 120.582 seconds serializing every
response cell to measure payload size. That measurement was stopped and the
response estimator was changed to sample rows and cells, with a 50% safety
margin and retry-on-limit behavior. The completed run above uses that estimator.
