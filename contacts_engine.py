"""
contacts_engine.py
==================
Personal contacts, via the People API.

Why this is worth building before more glamorous things
------------------------------------------------------
It is the gap users notice within an hour of cutover. Revision history going
missing is discovered weeks later by one person; an empty contact list is
discovered immediately by everyone, and it is the kind of loss that makes a
technically successful migration feel like a failure.

What migrates
-------------
Names, every email address and phone number, organisations, addresses,
birthdays, biographies, URLs and the contact groups ("labels") a contact
belongs to. Group membership is applied after the contacts exist, because a
group cannot reference a person who has not been created yet.

What does not
-------------
  * The "Other contacts" auto-collected list -- readable, but the API has no
    way to write into it. They reappear on their own as people send mail.
  * Contact photos. `updateContactPhoto` exists, so this is a limitation of
    this pass rather than of the API; it is skipped because a photo costs a
    request per contact and buys the least of anything here.
  * Directory contacts, which are not personal data at all -- they come from
    the target tenant's own directory once accounts exist.
"""

from __future__ import annotations

import logging

from resilience import PermanentAPIError, RateLimiter, retry_on_google_error

log = logging.getLogger(__name__)

# Everything worth copying off a Person. Requested explicitly because the API
# returns almost nothing by default.
PERSON_FIELDS = (
    "names,emailAddresses,phoneNumbers,organizations,addresses,biographies,"
    "birthdays,urls,memberships,nicknames,occupations,relations,userDefined"
)

# Fields accepted on create. `memberships` is deliberately absent: group
# membership is applied in a second pass, once both sides exist.
WRITE_FIELDS = [
    "names", "emailAddresses", "phoneNumbers", "organizations", "addresses",
    "biographies", "birthdays", "urls", "nicknames", "occupations",
    "relations", "userDefined",
]


class ContactsMigrator:
    def __init__(self, auth, db, settings, source_user: str, target_user: str):
        self.auth = auth
        self.db = db
        self.settings = settings
        self.source_user = source_user
        self.target_user = target_user
        self.src = auth.source_people(source_user)
        self.tgt = auth.target_people(target_user)
        self.limiter = RateLimiter(settings.per_user_qps)
        self.stats = {"contacts": 0, "groups": 0, "skipped": 0, "failed": 0}

    def _retry(self, fn):
        return retry_on_google_error(
            max_retries=self.settings.max_retries,
            base_delay=self.settings.base_backoff,
            max_delay=self.settings.max_backoff,
        )(fn)()

    # -- reading -------------------------------------------------------------
    def _iter_contacts(self):
        token = None
        while True:
            self.limiter.acquire()
            resp = self._retry(lambda t=token: self.src.people().connections()
                               .list(resourceName="people/me", pageSize=200,
                                     pageToken=t,
                                     personFields=PERSON_FIELDS).execute())
            for p in resp.get("connections", []):
                yield p
            token = resp.get("nextPageToken")
            if not token:
                return

    def _iter_groups(self):
        token = None
        while True:
            self.limiter.acquire()
            resp = self._retry(lambda t=token: self.src.contactGroups()
                               .list(pageSize=100, pageToken=t).execute())
            for g in resp.get("contactGroups", []):
                # SYSTEM_CONTACT_GROUP covers starred/myContacts etc, which
                # exist on the target already and cannot be created.
                if g.get("groupType") == "USER_CONTACT_GROUP":
                    yield g
            token = resp.get("nextPageToken")
            if not token:
                return

    # -- entry point ---------------------------------------------------------
    def run(self) -> dict:
        group_map = self._migrate_groups()
        for person in self._iter_contacts():
            self._migrate_contact(person, group_map)
        return dict(self.stats)

    def _migrate_groups(self) -> dict:
        """Contact groups first: a contact's membership references them."""
        mapping: dict[str, str] = {}
        try:
            groups = list(self._iter_groups())
        except (PermanentAPIError, RuntimeError) as exc:
            log.warning("[%s] contact groups unavailable: %s",
                        self.source_user, exc)
            return mapping

        for g in groups:
            name = g.get("name") or "Imported group"
            existing = self.db.get_target_id(self.source_user,
                                             g["resourceName"], "contact_group")
            if existing:
                mapping[g["resourceName"]] = existing
                self.stats["skipped"] += 1
                continue
            if self.settings.dry_run:
                self.stats["groups"] += 1
                continue
            try:
                self.limiter.acquire()
                created = self._retry(
                    lambda n=name: self.tgt.contactGroups().create(
                        body={"contactGroup": {"name": n}}).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, g["resourceName"],
                                  "contact_group", "FAILED", str(exc))
                self.stats["failed"] += 1
                continue
            mapping[g["resourceName"]] = created["resourceName"]
            self.db.record_mapping(self.source_user, g["resourceName"],
                                   created["resourceName"], "contact_group",
                                   source_name=name)
            self.stats["groups"] += 1
        return mapping

    def _migrate_contact(self, person: dict, group_map: dict) -> None:
        rid = person.get("resourceName")
        if self.db.get_target_id(self.source_user, rid, "contact"):
            self.stats["skipped"] += 1
            return
        if self.settings.dry_run:
            self.stats["contacts"] += 1
            return

        body = {f: person[f] for f in WRITE_FIELDS if person.get(f)}
        if not body:
            # A contact with no name, address or number is not a contact; it
            # is usually a stub left by an app. Recorded so the count still
            # reconciles rather than quietly differing by a handful.
            self.db.log_audit(self.source_user, rid, "contact",
                              "SKIPPED_EMPTY", "no writable fields")
            self.stats["skipped"] += 1
            return
        try:
            self.limiter.acquire()
            created = self._retry(lambda b=body: self.tgt.people().createContact(
                body=b).execute())
        except (PermanentAPIError, RuntimeError) as exc:
            self.db.log_audit(self.source_user, rid, "contact", "FAILED",
                              str(exc))
            self.stats["failed"] += 1
            return

        self.db.record_mapping(self.source_user, rid, created["resourceName"],
                               "contact")
        self.db.log_audit(self.source_user, rid, "contact", "SUCCESS")
        self.stats["contacts"] += 1
        self._apply_memberships(person, created, group_map)

    def _apply_memberships(self, person: dict, created: dict,
                           group_map: dict) -> None:
        """Put the new contact into the migrated equivalents of its groups."""
        wanted = []
        for m in person.get("memberships") or []:
            src_group = (m.get("contactGroupMembership") or {}).get(
                "contactGroupResourceName")
            if src_group and src_group in group_map:
                wanted.append(group_map[src_group])
        for group in wanted:
            try:
                self.limiter.acquire()
                self._retry(lambda g=group: self.tgt.contactGroups().members()
                            .modify(resourceName=g, body={
                                "resourceNamesToAdd": [created["resourceName"]]
                            }).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                # Was worth a row: a contact that arrives ungrouped looks
                # migrated and is unfindable in a list of nine thousand.
                self.db.log_audit(
                    self.source_user, person.get("resourceName"), "contact",
                    "FAILED", f"group membership {group} not applied: {exc}")
