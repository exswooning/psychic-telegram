"""
calendar_engine.py
===================
Module 4b: Calendar ingestion via `events.import_`.

Why `import_`, not `insert`
---------------------------
`events.insert` notifies attendees. Migrating five years of calendar history
with `insert` sends a fresh invitation for every past meeting to everyone who
ever attended it. `import_` preserves the original `iCalUID` and `organizer`
and has no notification path at all — the whole point of this module.

Recurring exceptions (a single moved/modified instance of a series) cannot be
imported directly: the target's recurring series generates its own instance
objects. Instead we look up the corresponding instance via
`events.instances(originalStart=...)` on the already-migrated target series,
then `events.patch(sendUpdates='none')` it with the modified fields.
"""

from __future__ import annotations

import logging

from google.auth.exceptions import RefreshError

from config import Settings
from resilience import PermanentAPIError, RateLimiter, retry_on_google_error

# See gmail_engine: an un-granted scope fails at token-mint time as a
# RefreshError, which the retry decorator never sees.
OPTIONAL_PASS_ERRORS = (PermanentAPIError, RuntimeError, RefreshError)

log = logging.getLogger(__name__)

_COPY_KEYS = ("description", "location", "status", "recurrence", "reminders",
              "visibility", "transparency", "colorId", "extendedProperties")
_PATCH_KEYS = ("summary", "description", "location", "start", "end", "status")


class CalendarMigrator:
    def __init__(self, auth, db, settings: Settings, source_user: str, target_user: str):
        self.auth = auth
        self.db = db
        self.settings = settings
        self.source_user = source_user
        self.target_user = target_user
        self.src = auth.source_calendar(source_user)
        self.tgt = auth.target_calendar(target_user)
        self.limiter = RateLimiter(settings.per_user_qps)
        self.stats = {"events": 0, "exceptions": 0, "failed": 0, "skipped": 0}

    def _retry(self, fn, label=None):
        return retry_on_google_error(
            max_retries=self.settings.max_retries,
            base_delay=self.settings.base_backoff,
            max_delay=self.settings.max_backoff,
            label=label or "calendar",
        )(fn)()

    # -- listing ----------------------------------------------------------------
    def _iter_events(self, updated_min: str | None, calendar_id: str = "primary"):
        token = None
        while True:
            self.limiter.acquire()
            kw = dict(calendarId=calendar_id, maxResults=250, singleEvents=False,
                      showDeleted=False, pageToken=token)
            if updated_min:
                kw["updatedMin"] = updated_min
            resp = self._retry(lambda kw=kw: self.src.events().list(**kw).execute())
            for e in resp.get("items", []):
                yield e
            token = resp.get("nextPageToken")
            if not token:
                return

    # -- translation --------------------------------------------------------------
    def _map_attendees(self, attendees: list[dict] | None) -> list[dict]:
        out = []
        for a in attendees or []:
            if a.get("resource"):
                continue  # room/equipment resources are tenant-specific; drop
            email = a.get("email")
            mapped = self.db.resolve_identity(email) or email
            entry = {k: v for k, v in a.items() if k not in ("email", "resource")}
            entry["email"] = mapped
            out.append(entry)
        return out

    def _map_attachments(self, attachments: list[dict] | None) -> list[dict]:
        out = []
        for a in attachments or []:
            mapped = self.db.get_target_id(self.source_user, a.get("fileId"), "file")
            if not mapped:
                continue  # a dead link is worse than none
            out.append({**a, "fileId": mapped})
        return out

    def _build_import_body(self, item: dict, tgt_cal_id: str = "primary") -> dict:
        body: dict = {
            "iCalUID": item["iCalUID"],
            "summary": item.get("summary", ""),
            "start": item["start"],
            "end": item["end"],
        }
        for k in _COPY_KEYS:
            if k in item:
                body[k] = item[k]

        organizer_email = (item.get("organizer") or {}).get("email")
        if organizer_email:
            body["organizer"] = {"email": self.db.resolve_identity(organizer_email) or organizer_email}

        attendees = self._map_attendees(item.get("attendees"))

        # Importing into a SECONDARY calendar is refused unless that calendar
        # is itself the organizer or an attendee ("The owner of the calendar
        # must either be the organizer or an attendee"). Adding it as an
        # attendee satisfies that while leaving the real organizer intact --
        # setting organizer to the calendar id also works but destroys the
        # original organizer, which is the one thing this module exists to keep.
        if tgt_cal_id != "primary":
            if not any(a.get("email") == tgt_cal_id for a in attendees):
                attendees = attendees + [{"email": tgt_cal_id,
                                         "responseStatus": "accepted"}]

        if attendees:
            body["attendees"] = attendees

        attachments = self._map_attachments(item.get("attachments"))
        if attachments:
            body["attachments"] = attachments

        return body

    # -- entry point ----------------------------------------------------------
    def run(self, delta: bool = False, updated_min: str | None = None) -> dict:
        self._migrate_calendar("primary", "primary",
                               updated_min if delta else None)
        if self.settings.migrate_secondary_calendars:
            self._migrate_secondary_calendars(updated_min if delta else None)
        return dict(self.stats)

    def _migrate_secondary_calendars(self, updated_min: str | None) -> None:
        """
        Every calendar the user owns beyond 'primary'.

        Only owned calendars are migrated: a subscribed or shared calendar
        belongs to someone else, and copying it would fork it into an
        unrelated second copy rather than re-subscribing the user to the
        original.
        """
        try:
            entries = self._retry(lambda: self.src.calendarList().list(
                minAccessRole="owner", showHidden=True,
            ).execute()).get("items", [])
        except (PermanentAPIError, RuntimeError) as exc:
            log.warning("[%s] could not list calendars: %s", self.source_user, exc)
            return

        for entry in entries:
            cal_id = entry.get("id")
            if entry.get("primary") or not cal_id:
                continue
            if entry.get("accessRole") != "owner":
                continue

            target_cal_id = self.db.get_target_id(self.source_user, cal_id, "calendar")
            if not target_cal_id:
                if self.settings.dry_run:
                    log.info("[DRY RUN] would create calendar %s",
                            entry.get("summary"))
                    self.stats["calendars"] = self.stats.get("calendars", 0) + 1
                    continue
                body = {"summary": entry.get("summary") or "Untitled",
                       "description": entry.get("description"),
                       "timeZone": entry.get("timeZone")}
                try:
                    created = self._retry(lambda b=body: self.tgt.calendars().insert(
                        body={k: v for k, v in b.items() if v is not None}
                    ).execute())
                except (PermanentAPIError, RuntimeError) as exc:
                    self.db.log_audit(self.source_user, cal_id, "calendar",
                                      "FAILED", str(exc))
                    self.stats["failed"] += 1
                    continue
                target_cal_id = created["id"]
                self.db.record_mapping(self.source_user, cal_id, target_cal_id,
                                       "calendar", source_name=entry.get("summary"))
                self.db.log_audit(self.source_user, cal_id, "calendar", "SUCCESS")
                self.stats["calendars"] = self.stats.get("calendars", 0) + 1

            self._migrate_calendar(cal_id, target_cal_id, updated_min)
            if self.settings.migrate_calendar_acls:
                self._sync_calendar_acl(cal_id, target_cal_id)

    def _sync_calendar_acl(self, src_cal_id: str, tgt_cal_id: str) -> None:
        """Translate a calendar's sharing rules, same identity mapping as
        Drive ACLs. 'default' and 'domain' scopes are rewritten to the target
        domain; unmapped internal users are dropped rather than leaked."""
        try:
            rules = self._retry(lambda: self.src.acl().list(
                calendarId=src_cal_id
            ).execute()).get("items", [])
        except OPTIONAL_PASS_ERRORS as exc:
            # Loud, not debug: the usual cause is that the source grant is
            # still calendar.readonly, which acl.list rejects outright. A
            # silently skipped ACL pass looks identical to a calendar that
            # simply had no sharing rules.
            log.warning(
                "[%s] could not read sharing rules for %s -- calendar ACLs "
                "NOT migrated for it. acl.list requires the full "
                "'calendar' scope on the source tenant (calendar.readonly is "
                "rejected). Error: %s",
                self.source_user, src_cal_id, exc,
            )
            return

        for rule in rules:
            scope = rule.get("scope") or {}
            stype, svalue = scope.get("type"), scope.get("value")
            if rule.get("role") == "owner":
                continue

            if stype == "user":
                mapped = self.db.resolve_identity(svalue)
                if not mapped:
                    if svalue and svalue.split("@")[-1].lower() == \
                            self.settings.source_domain.lower():
                        self.db.log_audit(
                            self.source_user, f"{src_cal_id}:{svalue}", "calendar_acl",
                            "SKIPPED_UNMAPPED_IDENTITY", f"no mapping for {svalue}")
                        continue
                    mapped = svalue
                body = {"scope": {"type": "user", "value": mapped},
                       "role": rule["role"]}
            elif stype == "domain":
                domain = svalue
                if domain and domain.lower() == self.settings.source_domain.lower():
                    domain = self.settings.target_domain
                body = {"scope": {"type": "domain", "value": domain},
                       "role": rule["role"]}
            elif stype == "default":
                body = {"scope": {"type": "default"}, "role": rule["role"]}
            else:
                continue

            if self.settings.dry_run:
                continue
            try:
                self._retry(lambda b=body: self.tgt.acl().insert(
                    calendarId=tgt_cal_id, body=b, sendNotifications=False,
                ).execute())
                self.stats["calendar_acls"] = self.stats.get("calendar_acls", 0) + 1
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, f"{src_cal_id}:{svalue}",
                                  "calendar_acl", "FAILED", str(exc))

    def _migrate_calendar(self, src_cal_id: str, tgt_cal_id: str,
                          updated_min: str | None) -> None:
        for item in self._iter_events(updated_min, calendar_id=src_cal_id):
            eid = item["id"]

            if item.get("recurringEventId"):
                self._handle_exception(item, tgt_cal_id)
                continue

            if not item.get("iCalUID"):
                self.db.log_audit(self.source_user, eid, "event", "SKIPPED_INVALID",
                                  "missing iCalUID")
                self.stats["skipped"] += 1
                continue

            if self.db.get_target_id(self.source_user, eid, "event"):
                self.stats["skipped"] += 1
                continue

            body = self._build_import_body(item, tgt_cal_id)

            if self.settings.dry_run:
                log.info("[DRY RUN] would import event %s", eid)
                self.stats["events"] += 1
                continue

            try:
                result = self._retry(lambda b=body: self.tgt.events().import_(
                    calendarId=tgt_cal_id, body=b, conferenceDataVersion=0,
                ).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, eid, "event", "FAILED", str(exc))
                self.stats["failed"] += 1
                continue

            self.db.record_mapping(self.source_user, eid, result["id"], "event")
            self.db.log_audit(self.source_user, eid, "event", "SUCCESS")
            self.stats["events"] += 1

    # -- recurring exceptions --------------------------------------------------------
    def _handle_exception(self, item: dict, tgt_cal_id: str = "primary") -> None:
        eid = item["id"]
        recurring_parent = item["recurringEventId"]

        if self.db.get_target_id(self.source_user, eid, "event"):
            self.stats["skipped"] += 1
            return

        target_master_id = self.db.get_target_id(self.source_user, recurring_parent, "event")
        if not target_master_id:
            self.db.log_audit(self.source_user, eid, "event", "SKIPPED_ORPHAN_EXCEPTION",
                              "master recurring event has not migrated")
            self.stats["skipped"] += 1
            return

        original_start = (item.get("originalStartTime") or {}).get("dateTime")
        try:
            inst_resp = self._retry(lambda: self.tgt.events().instances(
                eventId=target_master_id, originalStart=original_start,
            ).execute())
        except (PermanentAPIError, RuntimeError) as exc:
            self.db.log_audit(self.source_user, eid, "event", "FAILED", str(exc))
            self.stats["failed"] += 1
            return

        instances = inst_resp.get("items", [])
        if not instances:
            self.db.log_audit(self.source_user, eid, "event", "SKIPPED_ORPHAN_EXCEPTION",
                              "no matching instance on the target series")
            self.stats["skipped"] += 1
            return
        target_instance_id = instances[0]["id"]

        patch_body = {k: item[k] for k in _PATCH_KEYS if k in item}
        try:
            self._retry(lambda: self.tgt.events().patch(
                calendarId=tgt_cal_id, eventId=target_instance_id, body=patch_body,
                sendUpdates="none",
            ).execute())
        except (PermanentAPIError, RuntimeError) as exc:
            self.db.log_audit(self.source_user, eid, "event", "FAILED", str(exc))
            self.stats["failed"] += 1
            return

        self.db.record_mapping(self.source_user, eid, target_instance_id, "event")
        self.db.log_audit(self.source_user, eid, "event", "SUCCESS")
        self.stats["exceptions"] += 1
