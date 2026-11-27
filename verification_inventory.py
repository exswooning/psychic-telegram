"""Versioned per-user inventories used by one-to-one verification.

The contents of an inventory are only a cache of live API evidence. Callers
must replace it with a full read whenever its marker cannot be advanced.
"""

from __future__ import annotations

import json

from db import utc_now

INVENTORY_VERSION = 1


def incremental_enabled() -> bool:
    import os

    return os.getenv("VERIFY_INCREMENTAL", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _ensure(db) -> None:
    with db.write() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS verification_inventory_state (
            source_user TEXT NOT NULL,
            target_user TEXT NOT NULL,
            service TEXT NOT NULL,
            side TEXT NOT NULL,
            version INTEGER NOT NULL,
            marker TEXT NOT NULL,
            items_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(source_user, service, side)
        )""")
        state_columns = {row[1] for row in conn.execute("PRAGMA table_info(verification_inventory_state)")}
        if "items_count" not in state_columns:
            # Older development caches cannot prove their item set was complete; the count
            # mismatch makes load() choose a full API read before anything can pass.
            conn.execute("ALTER TABLE verification_inventory_state ADD COLUMN items_count INTEGER NOT NULL DEFAULT 0")
        conn.execute("""CREATE TABLE IF NOT EXISTS verification_inventory_item (
            source_user TEXT NOT NULL,
            service TEXT NOT NULL,
            side TEXT NOT NULL,
            item_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY(source_user, service, side, item_id)
        )""")


class InventoryCache:
    """SQLite storage for one source/target user pair and one inventory side."""

    def __init__(self, db, source_user: str, target_user: str, service: str, side: str):
        self.db = db
        self.source_user = source_user
        self.target_user = target_user
        self.service = service
        self.side = side

    def load(self) -> tuple[str, dict[str, dict]] | None:
        """Return a usable marker and inventory, or None for every stale/unknown shape."""
        _ensure(self.db)
        state = self.db.conn.execute(
            "SELECT target_user, version, marker, items_count FROM verification_inventory_state "
            "WHERE source_user=? AND service=? AND side=?",
            (self.source_user, self.service, self.side),
        ).fetchone()
        if (not state or state["target_user"] != self.target_user
                or state["version"] != INVENTORY_VERSION or not state["marker"]):
            return None
        rows = self.db.conn.execute(
            "SELECT item_id, payload FROM verification_inventory_item "
            "WHERE source_user=? AND service=? AND side=?",
            (self.source_user, self.service, self.side),
        ).fetchall()
        if state["items_count"] != len(rows):
            return None
        try:
            items = {row["item_id"]: json.loads(row["payload"]) for row in rows}
            if (len(items) != len(rows)
                    or any(not self._valid_item(item_id, item) for item_id, item in items.items())):
                return None
        except (TypeError, ValueError):
            return None
        return str(state["marker"]), items

    def _valid_item(self, item_id: str, item: dict) -> bool:
        if not isinstance(item, dict):
            return False
        if item.get("id") != item_id:
            return False
        if self.service == "gmail":
            return (
                isinstance(item.get("messageId"), str)
                and isinstance(item.get("from"), str)
                and isinstance(item.get("labels"), list)
                and all(isinstance(label, str) for label in item["labels"])
                and isinstance(item.get("internalDate"), str)
                and isinstance(item.get("threadId"), str)
                and isinstance(item.get("size"), int)
            )
        if self.service == "drive":
            return (isinstance(item.get("name"), str)
                    and isinstance(item.get("mimeType"), str)
                    and (item.get("md5Checksum") is None or isinstance(item.get("md5Checksum"), str)))
        return False

    def replace(self, marker: str, items: dict[str, dict]) -> None:
        """Atomically replace a full inventory and its checkpoint."""
        _ensure(self.db)
        now = utc_now()
        with self.db.write() as conn:
            conn.execute(
                "DELETE FROM verification_inventory_item WHERE source_user=? AND service=? AND side=?",
                (self.source_user, self.service, self.side),
            )
            conn.executemany(
                "INSERT INTO verification_inventory_item VALUES(?,?,?,?,?)",
                [(self.source_user, self.service, self.side, item_id,
                  json.dumps(payload, sort_keys=True, separators=(",", ":")))
                 for item_id, payload in items.items()],
            )
            conn.execute(
                """INSERT INTO verification_inventory_state
                   (source_user,target_user,service,side,version,marker,items_count,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_user,service,side) DO UPDATE SET
                   target_user=excluded.target_user, version=excluded.version,
                   marker=excluded.marker, items_count=excluded.items_count, updated_at=excluded.updated_at""",
                (self.source_user, self.target_user, self.service, self.side,
                 INVENTORY_VERSION, str(marker), len(items), now),
            )

    def apply(self, marker: str, upserts: dict[str, dict], deletes: set[str]) -> None:
        """Atomically apply a history/change-feed window and advance its marker."""
        _ensure(self.db)
        now = utc_now()
        with self.db.write() as conn:
            for item_id in deletes:
                conn.execute(
                    "DELETE FROM verification_inventory_item WHERE source_user=? AND service=? AND side=? AND item_id=?",
                    (self.source_user, self.service, self.side, item_id),
                )
            conn.executemany(
                """INSERT INTO verification_inventory_item VALUES(?,?,?,?,?)
                   ON CONFLICT(source_user,service,side,item_id) DO UPDATE SET payload=excluded.payload""",
                [(self.source_user, self.service, self.side, item_id,
                  json.dumps(payload, sort_keys=True, separators=(",", ":")))
                 for item_id, payload in upserts.items()],
            )
            items_count = conn.execute(
                "SELECT COUNT(*) FROM verification_inventory_item WHERE source_user=? AND service=? AND side=?",
                (self.source_user, self.service, self.side),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO verification_inventory_state
                   (source_user,target_user,service,side,version,marker,items_count,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_user,service,side) DO UPDATE SET
                   target_user=excluded.target_user, version=excluded.version,
                   marker=excluded.marker, items_count=excluded.items_count, updated_at=excluded.updated_at""",
                (self.source_user, self.target_user, self.service, self.side,
                 INVENTORY_VERSION, str(marker), items_count, now),
            )


def gmail_record(item_id: str, message: dict) -> dict:
    headers = {
        (header.get("name") or "").strip().lower(): (header.get("value") or "").strip()
        for header in (message.get("payload") or {}).get("headers", [])
    }
    return {
        "id": item_id,
        "messageId": headers.get("message-id", ""),
        "from": headers.get("from", ""),
        "labels": sorted(message.get("labelIds") or []),
        "internalDate": str(message.get("internalDate") or ""),
        "threadId": str(message.get("threadId") or ""),
        "size": int(message.get("sizeEstimate") or 0),
    }


def drive_record(file: dict) -> dict:
    return {
        "id": file["id"],
        "name": file.get("name"),
        "mimeType": file.get("mimeType"),
        "md5Checksum": file.get("md5Checksum"),
    }
