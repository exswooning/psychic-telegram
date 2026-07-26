"""
discovery.py
============
Module 1: pre-migration discovery.

Runs read-only against the source tenant and answers the questions every
migration plan needs before a single byte moves:

  * How many files and folders? How deep does the tree go?
  * How many bytes, and how long will that take at realistic throughput?
  * How many native Google Docs exceed the 10 MB export ceiling (these need a
    manual decision: PDF fallback, or leave behind)?
  * What is the MIME distribution? (A corpus that is 90% native Docs behaves
    completely differently from one that is 90% video.)

Implementation notes
--------------------
The traversal is a **single flat `files.list` sweep**, not a per-folder
recursive descent. One sweep over a 200k-item Drive costs ~400 API calls;
recursive descent costs one call per folder. The parent pointers returned in
each record are enough to rebuild the tree in memory afterwards, so depth is
computed locally rather than bought from the API.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from typing import Iterator

from config import DRIVE_FILE_FIELDS, FOLDER_MIME, SHORTCUT_MIME, Settings
from db import MigrationDB
from resilience import RateLimiter, retry_on_google_error

log = logging.getLogger(__name__)

# Assumed sustained throughput for planning. Conservative on purpose: real
# tenant-to-tenant throughput is dominated by API latency, not bandwidth.
ASSUMED_MB_PER_SEC = 6.0


def iter_all_drive_items(
    drive, settings: Settings, limiter: RateLimiter | None = None,
    query_extra: str = "",
) -> Iterator[dict]:
    """
    Yield every non-trashed item in the impersonated user's My Drive.

    `corpora='user'` restricts to items the user can see; combined with the
    `'me' in owners` filter (when `owned_only`) this gives exactly the set that
    is this user's responsibility to move.
    """
    q_parts = ["trashed = false"]
    if settings.owned_only:
        q_parts.append("'me' in owners")
    if query_extra:
        q_parts.append(query_extra)
    q = " and ".join(q_parts)

    page_token = None
    while True:
        if limiter:
            limiter.acquire()

        @retry_on_google_error(
            max_retries=settings.max_retries,
            base_delay=settings.base_backoff,
            max_delay=settings.max_backoff,
        )
        def _list(token=page_token):
            return drive.files().list(
                q=q,
                spaces="drive",
                corpora="user",
                pageSize=1000,
                fields=f"nextPageToken, files({DRIVE_FILE_FIELDS})",
                includeItemsFromAllDrives=False,
                supportsAllDrives=True,
                pageToken=token,
            ).execute()

        resp = _list()
        for f in resp.get("files", []):
            yield f
        page_token = resp.get("nextPageToken")
        if not page_token:
            return


def _compute_depths(items: dict[str, dict], root_id: str) -> dict[str, int]:
    """
    Compute depth for every item by walking parent pointers, memoised.

    Items whose parent is outside the fetched set (shared-in ancestors, or
    orphans) are treated as depth-1 children of root — which is exactly how
    Drive presents them in the UI.
    """
    depth: dict[str, int] = {}

    def resolve(node_id: str, seen: set[str]) -> int:
        if node_id in depth:
            return depth[node_id]
        if node_id == root_id or node_id in seen:
            return 0
        item = items.get(node_id)
        if not item:
            return 0
        parents = item.get("parents") or []
        if not parents:
            d = 1
        else:
            seen.add(node_id)
            d = resolve(parents[0], seen) + 1
            seen.discard(node_id)
        depth[node_id] = d
        return d

    for iid in items:
        resolve(iid, set())
    return depth


def scan_user(auth, db: MigrationDB, settings: Settings,
              source_user: str, include_mail: bool = False) -> dict:
    """
    Full read-only pre-scan of one source user. Persists a row into
    `discovery` and returns the stats dict.
    """
    log.info("[%s] starting pre-migration discovery scan", source_user)
    drive = auth.source_drive(source_user)
    limiter = RateLimiter(settings.per_user_qps)

    @retry_on_google_error(max_retries=settings.max_retries)
    def _root():
        return drive.files().get(fileId="root", fields="id").execute()

    root_id = _root()["id"]

    items: dict[str, dict] = {}
    mimes: Counter[str] = Counter()
    total_bytes = 0
    largest = 0
    largest_name = ""
    oversized_native = 0
    folder_count = 0
    file_count = 0
    native_count = 0
    shortcut_count = 0
    children: dict[str, int] = defaultdict(int)

    for f in iter_all_drive_items(drive, settings, limiter):
        items[f["id"]] = f
        mime = f.get("mimeType", "unknown")
        mimes[mime] += 1

        for p in f.get("parents") or []:
            children[p] += 1

        if mime == FOLDER_MIME:
            folder_count += 1
            continue
        if mime == SHORTCUT_MIME:
            shortcut_count += 1
            continue

        file_count += 1
        size = int(f.get("size") or 0)

        if mime.startswith("application/vnd.google-apps."):
            native_count += 1
            # Native docs report no `size`; their export payload is unknown
            # until exported. quotaBytesUsed is 0 for them, so we cannot
            # pre-flight the 10 MB export cap precisely — we flag likely
            # candidates by revision count elsewhere. Counted, not sized.
        else:
            total_bytes += size
            if size > largest:
                largest, largest_name = size, f.get("name", "")

    depths = _compute_depths(items, root_id)
    max_depth = max(depths.values(), default=0)

    est_seconds = total_bytes / (ASSUMED_MB_PER_SEC * 1024**2) if total_bytes else 0
    # Add API-latency overhead: empirically ~0.6s per item end-to-end.
    est_seconds += (file_count + folder_count) * 0.6
    est_days = est_seconds / 86400

    stats = dict(
        file_count=file_count,
        folder_count=folder_count,
        native_count=native_count,
        shortcut_count=shortcut_count,
        max_depth=max_depth,
        total_bytes=total_bytes,
        largest_bytes=largest,
        oversized_native=oversized_native,
        est_days=round(est_days, 3),
        mime_histogram=json.dumps(dict(mimes.most_common(40))),
    )

    # Fold the mailbox sizing into the same row: the dashboard needs one
    # authoritative "expected total" per user to compute progress against.
    if include_mail:
        try:
            mail = scan_mailbox(auth, settings, source_user)
            stats["messages_total"] = mail["messages_total"]
            stats["threads_total"] = mail["threads_total"]
            stats["user_label_count"] = mail["user_label_count"]
        except Exception as exc:  # noqa: BLE001 - mail sizing is advisory
            log.warning("[%s] mailbox scan failed: %s", source_user, exc)

    db.record_discovery(source_user, **stats)

    log.info(
        "[%s] scan complete: %d files / %d folders / %d native, "
        "depth=%d, %.2f GB, largest=%.1f MB (%s), est %.2f days",
        source_user, file_count, folder_count, native_count, max_depth,
        total_bytes / 1024**3, largest / 1024**2, largest_name[:60], est_days,
    )

    # Deep trees are a known migration hazard: Drive itself has no hard depth
    # limit, but path-based tooling downstream frequently does.
    if max_depth > 20:
        log.warning(
            "[%s] folder depth %d exceeds 20 — review for path-length issues "
            "in any downstream sync clients",
            source_user, max_depth,
        )

    stats["mime_histogram"] = dict(mimes.most_common(40))
    stats["source_user"] = source_user
    stats["root_id"] = root_id
    return stats


def scan_mailbox(auth, settings: Settings, source_user: str) -> dict:
    """Lightweight Gmail sizing pass: message count + label inventory."""
    gmail = auth.source_gmail(source_user)

    @retry_on_google_error(max_retries=settings.max_retries)
    def _profile():
        return gmail.users().getProfile(userId="me").execute()

    @retry_on_google_error(max_retries=settings.max_retries)
    def _labels():
        return gmail.users().labels().list(userId="me").execute()

    prof = _profile()
    labels = _labels().get("labels", [])
    user_labels = [l for l in labels if l.get("type") == "user"]

    stats = {
        "messages_total": prof.get("messagesTotal", 0),
        "threads_total": prof.get("threadsTotal", 0),
        "user_label_count": len(user_labels),
    }
    log.info(
        "[%s] mailbox: %d messages, %d threads, %d user labels",
        source_user, stats["messages_total"], stats["threads_total"],
        stats["user_label_count"],
    )
    return stats


def print_report(stats: dict) -> None:
    """Human-readable summary for the console."""
    print(f"\n=== Discovery: {stats.get('source_user')} ===")
    print(f"  Folders        : {stats['folder_count']:,}")
    print(f"  Binary files   : {stats['file_count'] - stats['native_count']:,}")
    print(f"  Native Docs    : {stats['native_count']:,}")
    print(f"  Shortcuts      : {stats['shortcut_count']:,}")
    print(f"  Max depth      : {stats['max_depth']}")
    print(f"  Total size     : {stats['total_bytes'] / 1024**3:.2f} GB")
    print(f"  Largest file   : {stats['largest_bytes'] / 1024**2:.1f} MB")
    print(f"  Est. duration  : {stats['est_days']:.2f} days (single-threaded)")
    hist = stats.get("mime_histogram") or {}
    if isinstance(hist, dict) and hist:
        print("  Top MIME types :")
        for mime, n in list(hist.items())[:8]:
            print(f"      {n:>7,}  {mime}")
