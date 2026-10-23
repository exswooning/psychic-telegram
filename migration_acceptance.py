"""Durable source manifests and bounded acceptance inside the original migration.

No upload success is a completion certificate. Failed/unsupported reads remain
explicit; stopped jobs retain pending manifest items for the next admitted run.
"""

from __future__ import annotations

import json
import uuid

from db import utc_now


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def ensure(db):
    with db.write() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS migration_acceptance_runs (
            run_id TEXT PRIMARY KEY, source_user TEXT, target_user TEXT, services TEXT,
            state TEXT, started_at TEXT, finished_at TEXT, report TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS migration_source_manifest (
            run_id TEXT, service TEXT, kind TEXT, source_id TEXT, source_snapshot TEXT,
            state TEXT, target_id TEXT, detail TEXT, PRIMARY KEY(run_id,service,kind,source_id))""")


def pages(call, key):
    token, seen = None, set()
    while True:
        response = call(token).execute()
        yield from response.get(key, [])
        token = response.get("nextPageToken")
        if not token:
            return
        if token in seen:
            raise RuntimeError("Source inventory repeated a page token")
        seen.add(token)


def inventory(auth, db, settings, source, target, services):
    """Read identities before copying. Values are source version/content metadata."""
    entries, errors = {}, {}

    def add(service, kind, key, item):
        entries[(service, kind, key)] = canonical(item)

    for service in sorted(services):
        try:
            if service in ("contacts", "tasks", "chat") and not getattr(settings, "migrate_" + service, False):
                raise RuntimeError("Requested scope is disabled: " + service)
            if service == "drive":
                svc = auth.source_drive(source)
                for item in pages(
                    lambda token: svc.files().list(
                        q="trashed=false",
                        spaces="drive",
                        pageSize=1000,
                        pageToken=token,
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                        fields="nextPageToken,files(id,name,mimeType,version,parents,modifiedTime,md5Checksum,ownedByMe,owners(emailAddress),driveId)",
                    ),
                    "files",
                ):
                    if item.get("driveId"):
                        continue
                    if item.get("ownedByMe") is False:
                        owners = {
                            x.get("emailAddress", "").rsplit("@", 1)[-1].lower()
                            for x in item.get("owners", [])
                        }
                        if (
                            not getattr(settings, "migrate_external_shares", False)
                            or not owners
                            or settings.source_domain.lower() in owners
                        ):
                            continue
                    mime = item.get("mimeType", "")
                    kind = (
                        "folder"
                        if mime.endswith(".folder")
                        else "shortcut"
                        if mime.endswith(".shortcut")
                        else "file"
                    )
                    add(service, kind, item["id"], item)
            elif service == "gmail":
                svc = auth.source_gmail(source)
                draft_messages = set()
                for draft in pages(
                    lambda t: (
                        svc.users()
                        .drafts()
                        .list(
                            userId="me",
                            maxResults=100,
                            pageToken=t,
                            includeSpamTrash=True,
                        )
                    ),
                    "drafts",
                ):
                    add(service, "draft", draft["id"], draft)
                    draft_messages.add(draft.get("message", {}).get("id"))
                skipped = {
                    r[0]
                    for r in db.conn.execute(
                        "SELECT item_id FROM audit_log WHERE source_user=? AND item_type='message' AND status='SKIPPED_CHAT'",
                        (source,),
                    )
                }
                for message in pages(
                    lambda t: (
                        svc.users()
                        .messages()
                        .list(
                            userId="me",
                            maxResults=500,
                            pageToken=t,
                            includeSpamTrash=True,
                        )
                    ),
                    "messages",
                ):
                    if (
                        message["id"] not in draft_messages
                        and message["id"] not in skipped
                    ):
                        add(service, "message", message["id"], message)
            elif service == "calendar":
                svc = auth.source_calendar(source)
                calendars = list(
                    pages(
                        lambda t: svc.calendarList().list(
                            pageToken=t, showHidden=True, maxResults=250
                        ),
                        "items",
                    )
                )
                if not any(c.get("primary") for c in calendars):
                    item = svc.calendarList().get(calendarId="primary").execute()
                    item["primary"] = True
                    calendars.insert(0, item)
                for cal in calendars:
                    primary = cal.get("primary")
                    cid = "primary" if primary else cal["id"]
                    if not primary and cal.get("accessRole") != "owner":
                        add(service, "calendar_subscription", cid, cal)
                        continue
                    if not primary and not getattr(
                        settings, "migrate_secondary_calendars", False
                    ):
                        continue
                    add(
                        service, "primary_calendar" if primary else "calendar", cid, cal
                    )
                    for event in pages(
                        lambda t, cid=cid: svc.events().list(
                            calendarId=cid,
                            pageToken=t,
                            maxResults=2500,
                            singleEvents=False,
                            showDeleted=False,
                        ),
                        "items",
                    ):
                        add(service, "event", cid + "::" + event["id"], event)
            elif service == "contacts" and getattr(settings, "migrate_contacts", False):
                svc = auth.source_people(source)
                for person in pages(
                    lambda t: (
                        svc.people()
                        .connections()
                        .list(
                            resourceName="people/me",
                            pageSize=1000,
                            pageToken=t,
                            personFields="names,emailAddresses,phoneNumbers,metadata,memberships",
                        )
                    ),
                    "connections",
                ):
                    add(service, "contact", person["resourceName"], person)
            elif service == "tasks" and getattr(settings, "migrate_tasks", False):
                svc = auth.source_tasks(source)
                for tasklist in pages(
                    lambda t: svc.tasklists().list(maxResults=100, pageToken=t), "items"
                ):
                    lid = tasklist["id"]
                    add(service, "task_list", lid, tasklist)
                    for task in pages(
                        lambda t, lid=lid: svc.tasks().list(
                            tasklist=lid,
                            maxResults=100,
                            pageToken=t,
                            showCompleted=True,
                            showHidden=True,
                            showDeleted=False,
                        ),
                        "items",
                    ):
                        add(service, "task", task["id"], {**task, "_list": lid})
            elif service == "chat" and getattr(settings, "migrate_chat", False):
                svc = auth.source_chat(source)
                for space in pages(
                    lambda t: svc.spaces().list(pageSize=100, pageToken=t), "spaces"
                ):
                    sid = space["name"]
                    add(service, "chat_space", sid, space)
                    for message in pages(
                        lambda t, sid=sid: (
                            svc.spaces()
                            .messages()
                            .list(parent=sid, pageSize=1000, pageToken=t)
                        ),
                        "messages",
                    ):
                        add(service, "chat_message", message["name"], message)
            elif service == "directory":
                import user_profiles

                src = (
                    auth.directory("source")
                    .users()
                    .get(userKey=source, projection="full", viewType="admin_view")
                    .execute()
                )
                mapping = {
                    r["source_email"]: r["target_email"] for r in db.all_identities()
                }
                add(
                    service,
                    "user",
                    source,
                    user_profiles.profile_body(src, target, mapping),
                )
        except Exception as exc:
            from verification_reads import error_detail

            errors[service] = error_detail(exc)
    return entries, errors


class Acceptance:
    def __init__(self, auth, db, settings, source, target, services):
        self.auth, self.db, self.settings = auth, db, settings
        self.source, self.target = source, target
        self.services = tuple(sorted(set(services) | {"directory"}))
        self.run_id = uuid.uuid4().hex
        ensure(db)
        with db.write() as c:
            c.execute(
                "INSERT INTO migration_acceptance_runs VALUES(?,?,?,?,?,?,NULL,NULL)",
                (
                    self.run_id,
                    source,
                    target,
                    canonical(self.services),
                    "INVENTORY",
                    utc_now(),
                ),
            )
        self.before, self.errors = inventory(
            auth, db, settings, source, target, self.services
        )
        with db.write() as c:
            for (service, kind, sid), payload in self.before.items():
                c.execute(
                    "INSERT INTO migration_source_manifest VALUES(?,?,?,?,?,'PENDING',NULL,NULL)",
                    (self.run_id, service, kind, sid, payload),
                )
            c.execute(
                "UPDATE migration_acceptance_runs SET state=?,report=? WHERE run_id=?",
                (
                    "BLOCKED" if self.errors else "COPYING",
                    canonical(
                        {
                            "inventoryErrors": self.errors,
                            "sourceItems": len(self.before),
                        }
                    ),
                    self.run_id,
                ),
            )

    def ready(self):
        return not self.errors

    def finish(self, *, repair=True, progress=print):
        import verify_sample as V
        import verification_repairs as R
        from resilience import shutdown_requested

        per, attempts, rounds = {}, [], 0
        cancelled = False
        wanted = tuple(
            s
            for s in self.services
            if s in V.ALL_SERVICES
            and (s != "chat" or getattr(self.settings, "migrate_chat", False))
        )
        pending = wanted
        cap = (
            max(0, min(2, int(getattr(self.settings, "acceptance_repair_rounds", 1)))) if repair else 0
        )
        while pending and not shutdown_requested():
            try:
                report = V.run(
                    self.auth, self.db, self.settings, [self.source], pending,
                    progress=progress, limit=None,
                )
            except V.VerificationCancelled:
                cancelled = True
                break
            per.update(report["users"][self.source])
            V.save_user_results(
                self.db, {"users": {self.source: report["users"][self.source]}}
            )
            verdict, reasons = V.verdict_of({self.source: per}, wanted)
            if verdict == "IDENTICAL" or rounds >= cap:
                break
            try:
                repaired = R.scoped_repair(
                    self.auth, self.db, self.settings, self.source, per
                )
            except V.VerificationCancelled:
                cancelled = True
                break
            attempts.append(repaired)
            rounds += 1
            pending = tuple(s for s in wanted if s in R.affected_services(repaired))
            if not pending:
                break
        after, read_errors = (
            inventory(
                self.auth,
                self.db,
                self.settings,
                self.source,
                self.target,
                self.services,
            )
            if not shutdown_requested() and not cancelled
            else (dict(self.before), {"run": "stopped"})
        )
        changes = [
            {
                "service": key[0],
                "kind": key[1],
                "id": key[2],
                "change": "added"
                if key not in self.before
                else "removed"
                if key not in after
                else "modified",
            }
            for key in sorted(set(self.before) | set(after))
            if self.before.get(key) != after.get(key)
        ]
        verdict, reasons = V.verdict_of({self.source: per}, wanted)
        # Service success cannot certify an item that never reached the ledger.
        mapping_kinds = {"file", "folder", "shortcut", "message", "draft", "calendar",
                         "event", "contact", "contact_group", "task_list", "task",
                         "chat_space", "chat_message"}
        mapping_gaps = [
            {"service": service, "kind": kind, "id": sid}
            for service, kind, sid in sorted(set(self.before) | set(after))
            if kind in mapping_kinds and not self.db.get_target_id(self.source, sid, kind)
        ]
        # Persist independent manifest findings through the same durable queue
        # and saved service results as content findings. They cannot be hidden
        # behind an earlier successful service verdict.
        for gap in mapping_gaps:
            row = per.setdefault(gap["service"], V.Verifier._blank())
            item = {"id": gap["id"], "type": gap["kind"], "status": "UNMAPPED_SOURCE",
                    "error": "Pre-copy manifest item has no destination mapping"}
            if not any(i.get("id") == gap["id"] for i in row["notCopied"]):
                row["notCopied"].append(item)
            row["coverageComplete"] = False
        for change in changes:
            row = per.setdefault(change["service"], V.Verifier._blank())
            row["errors"].append("Source changed during migration: " + canonical(change))
            row["coverageComplete"] = False
        for service, error in {**self.errors, **read_errors}.items():
            if service in wanted:
                row = per.setdefault(service, V.Verifier._blank())
                row["errors"].append("Source manifest unread: " + str(error))
                row["coverageComplete"] = False
        if per and not cancelled and not shutdown_requested():
            V.save_user_results(self.db, {"users": {self.source: per}})
        verdict, reasons = V.verdict_of({self.source: per}, wanted)
        complete = (
            verdict == "IDENTICAL"
            and not mapping_gaps
            and not cancelled
            and not changes
            and not read_errors
            and not self.errors
            and not shutdown_requested()
        )
        state = (
            "ACCEPTED"
            if complete
            else "INTERRUPTED"
            if shutdown_requested() or cancelled
            else "NEEDS_ATTENTION"
        )
        result = {
            "runId": self.run_id,
            "complete": complete,
            "state": state,
            "verdict": verdict,
            "reasons": reasons,
            "services": list(wanted),
            "sourceItems": len(self.before),
            "sourceChanges": changes,
            "mappingGaps": mapping_gaps,
            "inventoryErrors": {**self.errors, **read_errors},
            "repairRounds": rounds,
            "repairs": attempts,
            "scope": "selected migration scope; only explicitly implemented feature evidence is certified",
        }
        with self.db.write() as c:
            for key in set(self.before) | set(after):
                service, kind, sid = key
                tid = self.db.get_target_id(self.source, sid, kind)
                evidence = per.get(service)
                good = (
                    bool(evidence)
                    and V.verdict_of({self.source: {service: evidence}}, (service,))[0]
                    == "IDENTICAL"
                )
                item_state = (
                    "VALIDATED"
                    if good
                    and (kind not in mapping_kinds or bool(tid))
                    and not cancelled and not shutdown_requested()
                    and self.before.get(key) == after.get(key)
                    and service not in read_errors
                    else "RECHECK_REQUIRED"
                )
                c.execute(
                    "INSERT INTO migration_source_manifest VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,service,kind,source_id) DO UPDATE SET state=excluded.state,target_id=excluded.target_id,detail=excluded.detail",
                    (
                        self.run_id,
                        service,
                        kind,
                        sid,
                        after.get(key) or self.before.get(key),
                        item_state,
                        tid,
                        canonical(
                            {
                                "serviceVerdict": V.verdict_of(
                                    {self.source: {service: evidence}}, (service,)
                                )[0]
                                if evidence
                                else "NOT_VERIFIED"
                            }
                        ),
                    ),
                )
            c.execute(
                "UPDATE migration_acceptance_runs SET state=?,finished_at=?,report=? WHERE run_id=?",
                (state, utc_now(), canonical(result), self.run_id),
            )
        return result


def dependency_preflight(auth, db, settings, source, target):
    """Names and identity mappings are prerequisites, never guessed from filenames."""
    import user_profiles

    result = user_profiles.sync_profiles(
        auth,
        db,
        settings,
        [(source, target)],
        apply=not settings.dry_run,
        create_missing=False,
        progress=lambda _: None,
    )
    failures = [
        r.get("error") or r.get("status")
        for r in result["profiles"]
        if r.get("status") not in ("MATCHED", "UPDATED")
    ]
    if failures:
        raise RuntimeError("Profile dependency unresolved: " + "; ".join(failures))
    if getattr(settings, "migrate_groups", False):
        from tenant_verify import TenantCheck

        check = TenantCheck(auth, db, settings).groups()
        if check["status"] != "IDENTICAL":
            raise RuntimeError(
                "Group membership dependencies unresolved: "
                + canonical(check["differences"][:5])
            )
    return result
