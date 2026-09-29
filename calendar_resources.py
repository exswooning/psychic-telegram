"""
calendar_resources.py
=====================
Rooms and equipment: recreate the source tenant's calendar resources on the
target and map their addresses, so a meeting booked in a room still names a
room instead of losing it.

A resource's address is minted by Google per tenant
(c_...@resource.calendar.google.com), so it cannot be kept -- it is mapped,
the way a user's address is. The buildings and features a room names are
created first: a room cannot point at a building the target does not have.

Idempotent: a room whose resourceId already exists on the target is mapped,
not created again. Needs MIGRATE_RESOURCES and the directory resource scopes
(read on the source, write on the target).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

LEDGER_USER = "__resources__"

_ROOM_KEYS = ("resourceId", "resourceName", "resourceType", "resourceDescription",
              "userVisibleDescription", "capacity", "buildingId", "floorName",
              "floorSection", "resourceCategory", "featureInstances")
_BUILDING_KEYS = ("buildingId", "buildingName", "description", "floorNames",
                  "coordinates", "address")


def resource_target(db, email: str | None) -> str | None:
    """The target address of a source room or piece of equipment, or None."""
    return db.get_target_id(LEDGER_USER, (email or "").lower(), "resource") if email else None


def _all(call, key: str) -> list[dict]:
    out, token = [], None
    while True:
        resp = call(token).execute()
        out += resp.get(key, [])
        token = resp.get("nextPageToken")
        if not token:
            return out


class ResourceMigrator:
    def __init__(self, auth, db, settings):
        self.auth, self.db, self.settings = auth, db, settings
        self.stats = {"buildings": 0, "features": 0, "rooms": 0, "mapped": 0, "failed": 0}

    def _res(self, tenant: str):
        return self.auth.directory(tenant).resources()

    def migrate(self) -> dict:
        src, tgt = self._res("source"), self._res("target")
        cust = "my_customer"
        have_b = {b["buildingId"] for b in _all(
            lambda t: tgt.buildings().list(customer=cust, pageToken=t), "buildings")}
        for b in _all(lambda t: src.buildings().list(customer=cust, pageToken=t), "buildings"):
            if b["buildingId"] in have_b or self.settings.dry_run:
                continue
            try:
                tgt.buildings().insert(customer=cust, body={
                    k: b[k] for k in _BUILDING_KEYS if k in b}).execute()
                self.stats["buildings"] += 1
            except Exception as exc:      # noqa: BLE001 - its rooms fail and say so
                log.warning("building %s not created: %s", b.get("buildingName"), exc)

        have_f = {f["name"] for f in _all(
            lambda t: tgt.features().list(customer=cust, pageToken=t), "features")}
        for f in _all(lambda t: src.features().list(customer=cust, pageToken=t), "features"):
            if f["name"] in have_f or self.settings.dry_run:
                continue
            try:
                tgt.features().insert(customer=cust, body={"name": f["name"]}).execute()
                self.stats["features"] += 1
            except Exception as exc:      # noqa: BLE001
                log.warning("feature %s not created: %s", f["name"], exc)

        have = {r["resourceId"]: r for r in _all(
            lambda t: tgt.calendars().list(customer=cust, pageToken=t), "items")}
        for r in _all(lambda t: src.calendars().list(customer=cust, pageToken=t), "items"):
            src_email = (r.get("resourceEmail") or "").lower()
            made = have.get(r["resourceId"])
            if made is None:
                if self.settings.dry_run:
                    continue
                try:
                    made = tgt.calendars().insert(customer=cust, body={
                        k: r[k] for k in _ROOM_KEYS if k in r}).execute()
                    self.stats["rooms"] += 1
                except Exception as exc:      # noqa: BLE001
                    self.db.log_audit(LEDGER_USER, src_email, "resource", "FAILED", str(exc))
                    self.stats["failed"] += 1
                    continue
            if src_email and made.get("resourceEmail"):
                self.db.record_mapping(LEDGER_USER, src_email,
                                       made["resourceEmail"].lower(), "resource",
                                       source_name=r.get("resourceName"))
                self.stats["mapped"] += 1
        return self.stats
