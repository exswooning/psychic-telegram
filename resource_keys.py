"""
resource_keys.py
================
Drive resource keys, remembered from listings and sent with every later read.

Since Google's 2021 link-sharing update, an older file reached through a link-shared
folder can only be opened by id when the request carries its resource key
(`X-Goog-Drive-Resource-Keys: <id>/<key>`). Without it Google answers 404 "File not
found" -- live, dharan -> s. (2026-10-25): 94 of 194 failures were exactly this (downloads,
permission and comment reads, shortcut targets), e.g. "Local Level Budget Format.xlsx" in a
folder shared with info@ from outside, readable at once with the key.

Listing a folder returns each child's key (when `resourceKey` is asked for); every source
read goes through auth.ReadOnlyDrive, which asks for it and sends it from then on. Keys are
per file, not per user, so one process-wide map serves every worker.
"""
from __future__ import annotations

import sqlite3
import threading

KEYS: dict[str, str] = {}
HEADER = "X-Goog-Drive-Resource-Keys"
# Kept in the account's own ledger too: a repair, a re-check or a later run reads a file it
# never listed itself (a share retried on a copied file, a One-to-one comparison).
_local = threading.local()


def _conn(db_path: str | None):
    if not db_path:
        return None
    cache = getattr(_local, "conns", None) or {}
    _local.conns = cache
    if db_path not in cache:
        try:
            c = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
            c.execute("CREATE TABLE IF NOT EXISTS resource_keys (file_id TEXT PRIMARY KEY, key TEXT NOT NULL)")
            cache[db_path] = c
        except sqlite3.Error:
            cache[db_path] = None
    return cache[db_path]


def lookup(file_id: str, db_path: str | None = None) -> str | None:
    if not file_id:
        return None
    if file_id in KEYS:
        return KEYS[file_id]
    c = _conn(db_path)
    if c is not None:
        try:
            row = c.execute("SELECT key FROM resource_keys WHERE file_id=?", (file_id,)).fetchone()
        except sqlite3.Error:
            row = None
        if row:
            KEYS[file_id] = row[0]
            return row[0]
    return None


def remember(response, db_path: str | None = None) -> None:
    """Keep the keys a files.list / files.get response carries, shortcut targets included."""
    if not isinstance(response, dict):
        return
    new = {}
    for f in response.get("files") or ([response] if "id" in response else []):
        if not isinstance(f, dict):
            continue
        if f.get("id") and f.get("resourceKey"):
            new[f["id"]] = f["resourceKey"]
        sd = f.get("shortcutDetails") or {}
        if sd.get("targetId") and sd.get("targetResourceKey"):
            new[sd["targetId"]] = sd["targetResourceKey"]
    fresh = {k: v for k, v in new.items() if KEYS.get(k) != v}
    KEYS.update(new)
    c = _conn(db_path) if fresh else None
    if c is not None:
        try:
            with c:
                c.executemany("INSERT OR REPLACE INTO resource_keys(file_id, key) VALUES (?, ?)",
                              list(fresh.items()))
        except sqlite3.Error:
            pass            # advisory: the in-process map still serves this run


def with_key_field(collection: str, method: str, fields):
    """Ask for resourceKey wherever a partial response would leave it out."""
    if collection != "files" or not fields or "resourceKey" in fields:
        return fields
    if method == "list":
        return fields.replace("files(", "files(resourceKey,", 1) if "files(" in fields else fields
    if method == "get":
        return f"{fields},resourceKey"
    return fields


def keyed(fn, collection: str, method: str, db_path: str | None = None):
    """Wrap one read method: ask for keys, send the file's key, remember what comes back."""
    def call(*a, **kw):
        if "fields" in kw:
            kw["fields"] = with_key_field(collection, method, kw["fields"])
        req = fn(*a, **kw)
        key = lookup(kw.get("fileId") or "", db_path)
        headers = getattr(req, "headers", None)
        if key and isinstance(headers, dict):
            mine = f"{kw['fileId']}/{key}"
            headers[HEADER] = f"{headers[HEADER]},{mine}" if headers.get(HEADER) else mine
        run = getattr(req, "execute", None)
        if callable(run):
            def execute(*ea, **ek):
                out = run(*ea, **ek)
                remember(out, db_path)
                return out
            try:
                req.execute = execute
            except AttributeError:
                pass
        return req
    return call
