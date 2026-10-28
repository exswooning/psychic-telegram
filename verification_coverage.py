"""Explicit evidence for migration coverage; missing checks never become passes."""

from __future__ import annotations

import json

VERSION = 2


def check(key, label, status, checked=0, matched=0, note=None, required=True):
    return {
        "key": key,
        "label": label,
        "status": status,
        "checked": checked,
        "matched": matched,
        "required": required,
        "note": note,
    }


def pages(v, call, key):
    token, seen, rows = None, set(), []
    while True:
        response = v._x(lambda: call(token).execute())
        rows.extend(response.get(key, []))
        token = response.get("nextPageToken")
        if not token:
            return rows
        if token in seen:
            raise RuntimeError(f"{key} inventory repeated a page token")
        seen.add(token)


def add(v, result, key, label, fn):
    try:
        result.setdefault("checks", []).append(fn())
    except Exception as exc:
        from verification_reads import error_detail

        detail = error_detail(exc)
        result.setdefault("checks", []).append(
            check(key, label, "INCOMPLETE", note=detail)
        )
        result["errors"].append(f"{label}: {detail}")


def drive(v, result):
    src = v.auth.source_drive(v.src_user)
    external_items = []

    def inventory():
        files = pages(
            v,
            lambda token: src.files().list(
                q="trashed=false",
                spaces="drive",
                pageSize=1000,
                pageToken=token,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields="nextPageToken,files(id,name,mimeType,parents,shortcutDetails,ownedByMe,owners(emailAddress),driveId,capabilities(canShare))",
            ),
            "files",
        )
        known = {
            s for kind in ("file", "folder", "shortcut") for s, _, _ in v._pairs(kind)
        }
        in_scope = []
        owned_items = []
        excluded = 0
        for file in files:
            owners = [
                p.get("emailAddress", "").rsplit("@", 1)[-1].lower()
                for p in file.get("owners", [])
            ]
            carried = file.get("ownedByMe") is False
            if file.get("driveId"):
                excluded += 1  # tenant/shared-drive check owns this scope
                continue
            # Owned by the target domain: Bitport's own copy, still readable by the
            # source user who made it (a server-side copy keeps "anyone with the
            # link"). Live, dharan: 118 such copies read as unmapped source files.
            ours = (v.settings.source_domain.lower(), (v.settings.target_domain or "").lower())
            if carried and not (
                getattr(v.settings, "migrate_external_shares", False)
                and owners
                and all(x not in ours for x in owners)
            ):
                excluded += 1
                continue
            in_scope.append(file["id"])
            if carried:
                external_items.append(file)
            if not carried:
                owned_items.append(file)
        from migration_checks import accounted_for, drive_decisions

        decided = drive_decisions(v.db, v.src_user)
        reasoned = {f for f in set(in_scope) - known if accounted_for(*decided.get(f, (None, None)))}
        missing = set(in_scope) - known - reasoned
        old = {i.get("id") for i in result["notCopied"]}
        for fid in sorted(missing - old):
            result["notCopied"].append(
                {
                    "type": "file",
                    "id": fid,
                    "status": "UNMAPPED_SOURCE",
                    "error": "Live in-scope Drive item has no source-to-target migration mapping",
                }
            )
        result["nativeSourceCount"] = sum(f.get("mimeType","").startswith("application/vnd.google-apps.") and not f.get("mimeType","").endswith((".folder",".shortcut")) for f in files if f["id"] in in_scope)
        result["sourceInventory"] = {
            "inScope": len(in_scope),
            "excludedSharedOrTenantItems": excluded,
            "unmapped": len(missing),
            "notCopiedByDecision": len(reasoned),
        }
        result["emptySource"] = not in_scope

        def structure():
            if not owned_items:
                return check(
                    "drive.structure",
                    "Exact parent folders, names and shortcut targets",
                    "EMPTY",
                )
            tgt = v.auth.target_drive(v.tgt_user)
            targets = {
                f["id"]: f
                for f in pages(
                    v,
                    lambda token: tgt.files().list(
                        q="trashed=false",
                        spaces="drive",
                        pageSize=1000,
                        pageToken=token,
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                        fields="nextPageToken,files(id,name,mimeType,parents,shortcutDetails)",
                    ),
                    "files",
                )
            }
            source_root = v._x(
                lambda: src.files().get(fileId="root", fields="id").execute()
            )["id"]
            target_root = v._x(
                lambda: tgt.files().get(fileId="root", fields="id").execute()
            )["id"]
            mapping = {
                a: b
                for kind in ("file", "folder", "shortcut")
                for a, b, _ in v._pairs(kind)
            }
            matched = 0
            for item in owned_items:
                sid = item["id"]
                tid = mapping.get(sid)
                target = targets.get(tid)
                diffs = []
                if not target:
                    diffs.append("mapped Drive item is absent from target inventory")
                else:
                    for field in ("name", "mimeType"):
                        if item.get(field) != target.get(field):
                            diffs.append("Drive " + field + " differs")
                    wanted = {
                        target_root
                        if parent == source_root
                        else mapping.get(parent) or v.db.target_for_source_id(parent)
                        for parent in item.get("parents", [])
                    }
                    if None in wanted or wanted != set(target.get("parents", [])):
                        diffs.append("mapped parent folder IDs differ")
                    shortcut = item.get("shortcutDetails", {})
                    if shortcut:
                        source_link = shortcut.get("targetId")
                        got = target.get("shortcutDetails", {}).get("targetId")
                        # Any genuine copy of the file it points to: one file shared
                        # with many users has a copy per user (see copy_lookup).
                        copies = (v.db.targets_for_source_id(source_link)
                                  if hasattr(v.db, "targets_for_source_id") else [])
                        expected = v.db.target_for_source_id(source_link) or source_link
                        if got != expected and got not in copies:
                            diffs.append("shortcut target differs")
                if diffs:
                    result["differences"].append(
                        {
                            "source": sid,
                            "target": tid,
                            "item": item.get("name", sid),
                            "scope": "drive-structure",
                            "diffs": diffs,
                        }
                    )
                else:
                    matched += 1
            return check(
                "drive.structure",
                "Exact parent folders, names and shortcut targets",
                "DIFFERENCES"
                if matched != len(owned_items)
                else "MATCHED"
                if owned_items
                else "EMPTY",
                len(owned_items),
                matched,
            )

        add(v, result, "drive.structure", "Drive structure inventory", structure)
        return check(
            "drive.inventory",
            "Source item coverage",
            "DIFFERENCES" if missing else "MATCHED" if in_scope else "EMPTY",
            len(in_scope),
            len(in_scope) - len(missing),
            "Shared Drive contents and colleague-owned items have separate owners/scopes.",
        )

    add(v, result, "drive.inventory", "Source item coverage", inventory)
    if external_items:
        from external_sharing import inspect

        def external_permissions():
            evidence = inspect(v, result, external_items)
            statuses = {row["status"] for row in evidence["items"]}
            status = ("INCOMPLETE" if "INCOMPLETE" in statuses else
                      "DIFFERENCES" if "DIFFERENCES" in statuses else
                      "NOT_VERIFIED" if evidence["unverified"] or evidence["notChecked"] else
                      "MATCHED")
            return check("drive.externalSharing", "External original access and shortcut placement",
                         status, evidence["compared"],
                         sum(row["status"] == "MATCHED" for row in evidence["items"]),
                         "Same original ID, effective user role and capabilities, shortcut placement, and private fallback are checked independently.")

        add(v, result, "drive.externalSharing", "External file sharing inspection",
            external_permissions)
    result.setdefault("checks", []).append(
        check(
            "drive.items",
            "Mapped item content, names and metadata",
            "INCOMPLETE"
            if result["errors"]
            else "DIFFERENCES"
            if any(
                result[k] for k in ("differences", "missing", "notCopied", "duplicates")
            )
            else "EMPTY"
            if result.get("emptySource")
            else "MATCHED"
            if result["checked"]
            else "NOT_VERIFIED",
            result["checked"],
            result["identical"],
        )
    )
    sharing = result.get("sharing") or {}
    result.setdefault("checks", []).append(
        check(
            "drive.permissions",
            "File and folder share access",
            "EMPTY"
            if result.get("emptySource")
            else "INCOMPLETE"
            if any("sharing" in e for e in result["errors"])
            else "DIFFERENCES"
            if any(
                any("grant" in d for d in item.get("diffs", []))
                for item in result["differences"]
            )
            else "NOT_VERIFIED"
            if sharing.get("notReproduced") or result.get("not_owner") or (
                result.get("carriedIn") and (
                    not result.get("externalSharing")
                    or result["externalSharing"].get("unverified")
                    or result["externalSharing"].get("notChecked")))
            else "MATCHED"
            if sharing.get("filesCompared", 0)
            else "NOT_VERIFIED",
            sharing.get("filesCompared", 0),
            sharing.get("matched", 0),
            f"{sharing.get('matched', 0)} grants matched; {sharing.get('notReproduced', 0)} deliberately skipped grants "
            f"and permissions on {result.get('not_owner', 0)} unreadable source items and "
            f"{(result.get('externalSharing') or {}).get('matched', 0)} external originals have equivalent access and verified shortcuts; "
            f"{(result.get('externalSharing') or {}).get('unverified', result.get('carriedIn', 0))} remain unverified. "
            "External originals keep their owners and collaborators; retained private copies are content fallbacks.",
        )
    )
    native_checked=sum(bool(e.get("nativeFeaturesChecked")) for e in v.evidence if e.get("service")=="drive")
    result["checks"].append(check("drive.native", "Readable native content, formatting and structures",
        "EMPTY" if result.get("nativeSourceCount")==0 else "INCOMPLETE" if native_checked!=result.get("nativeSourceCount") else "DIFFERENCES" if any(e.get("nativeFeaturesMatched") is False for e in v.evidence if e.get("service")=="drive") else "MATCHED",
        result.get("nativeSourceCount",0),native_checked,
        note="Native API fields only. Embedded image bytes and unsupported Google types block this check; revision history is separate."))
    if getattr(v.settings,"migrate_comments",False):
        def comments():
            from migration_comments import list_threads,differences
            compared=matched=0
            for sid,tid,_ in v._pairs("file",sample=True):
                a=list_threads(src,sid,v._x)
                b=list_threads(v.auth.target_drive(v.tgt_user),tid,v._x)
                diffs=differences(a,b,v.db.target_for_source_id)
                compared+=len(a);matched+=len(a) if not diffs else 0
                if diffs:result["differences"].append({"source":sid,"target":tid,"scope":"drive-comments","diffs":diffs})
            return check("drive.comments","Comment thread text, replies, anchors and resolution",
                "DIFFERENCES" if any(i.get("scope")=="drive-comments" for i in result["differences"]) else "MATCHED" if compared else "EMPTY",compared,matched,
                note="Original author/date are retained as attribution text when replayed; native author identities/history are not reproduced.")
        add(v,result,"drive.comments","Comment threads",comments)
    else:
        result["checks"].append(check("drive.comments","Comment threads","NOT_SELECTED",required=False))
    result["checks"].append(
        check(
            "drive.revisions",
            "Revision history",
            "NOT_VERIFIED",
            note="Revision history is not copied by the migration methods in use.",
            required=False,
        )
    )


def _calendar_key(v, rule):
    scope = rule.get("scope") or {}
    kind, value = scope.get("type"), scope.get("value", "")
    if rule.get("role") == "owner":
        return None
    if kind in ("user", "group"):
        original = value
        value = v._translate(value)
        if (
            original.rsplit("@", 1)[-1].lower() == v.settings.source_domain.lower()
            and value == original.lower()
        ):
            raise RuntimeError(
                "Calendar grantee has no destination identity mapping: " + original
            )
    elif kind == "domain" and value.lower() == v.settings.source_domain.lower():
        value = v.settings.target_domain.lower()
    return kind, value.lower(), rule.get("role")


def calendar(v, result):
    from calendar_engine import list_settings

    src, tgt = v.auth.source_calendar(v.src_user), v.auth.target_calendar(v.tgt_user)
    calendars = pages(
        v,
        lambda token: src.calendarList().list(
            maxResults=250, showHidden=True, pageToken=token
        ),
        "items",
    )
    target_entries = {
        c["id"]: c
        for c in pages(
            v,
            lambda token: tgt.calendarList().list(
                maxResults=250, showHidden=True, pageToken=token
            ),
            "items",
        )
    }
    target_primary = next(
        (c for c in target_entries.values() if c.get("primary")), None
    )
    if not any(c.get("primary") for c in calendars):
        calendars.insert(
            0, v._x(lambda: src.calendarList().get(calendarId="primary").execute())
        )
        calendars[0]["primary"] = True
    mapping = {s: t for s, t, _ in v._pairs("calendar")}
    acl_selected = bool(
        getattr(v.settings, "migrate_calendar_acls", False)
        or v.db.conn.execute(
            "SELECT 1 FROM audit_log WHERE source_user=? AND item_type='calendar_acl' LIMIT 1",
            (v.src_user,),
        ).fetchone()
    )
    events_known = {s for s, _, _ in v._pairs("event")}
    stats = {
        "events": 0,
        "calendars": 0,
        "followed": 0,
        "grants": 0,
        "matchedGrants": 0,
        "settingsMatched": 0,
        "subscriptionsMatched": 0,
    }
    result["checks"] = result.get("checks", [])
    for entry in calendars:
        cid = "primary" if entry.get("primary") else entry["id"]
        owned = entry.get("primary") or entry.get("accessRole") == "owner"
        if not owned:
            stats["followed"] += 1
            destination = mapping.get(cid) or v._translate(cid)
            found = target_entries.get(destination)
            diffs = []
            if not found:
                diffs.append("followed/shared calendar is not subscribed on target")
            elif list_settings(entry)[0] != list_settings(found)[0]:
                diffs.append("followed calendar display/reminder settings differ")
            if not diffs:
                stats["subscriptionsMatched"] += 1
            if diffs:
                result["differences"].append(
                    {
                        "item": entry.get("summary", cid),
                        "source": cid,
                        "target": destination,
                        "scope": "subscription",
                        "diffs": diffs,
                    }
                )
            continue
        if cid != "primary" and not (
            getattr(v.settings, "migrate_secondary_calendars", False) or cid in mapping
        ):
            result["checks"].append(
                check(
                    "calendar.secondary." + cid,
                    entry.get("summary", cid),
                    "NOT_SELECTED",
                    note="Secondary calendar migration was not selected.",
                    required=False,
                )
            )
            continue
        stats["calendars"] += 1
        destination = "primary" if cid == "primary" else mapping.get(cid)
        if not destination:
            result["notCopied"].append(
                {
                    "type": "calendar",
                    "id": cid,
                    "status": "UNMAPPED_SOURCE",
                    "error": "Owned source calendar has no target mapping",
                }
            )
            continue
        found = (
            target_primary
            if destination == "primary"
            else target_entries.get(destination)
        )
        if not found:
            result["missing"].append(
                {
                    "source": cid,
                    "target": destination,
                    "name": entry.get("summary"),
                    "why": "Target calendar/list entry is missing",
                }
            )
            continue
        if list_settings(entry)[0] != list_settings(found)[0]:
            result["differences"].append(
                {
                    "item": entry.get("summary", cid),
                    "source": cid,
                    "target": destination,
                    "scope": "calendar-settings",
                    "diffs": ["calendar display/reminder settings differ"],
                }
            )
        if cid != "primary":
            for field in ("summary", "description", "timeZone"):
                if (entry.get(field) or "") != (found.get(field) or ""):
                    result["differences"].append(
                        {
                            "item": entry.get("summary", cid),
                            "source": cid,
                            "target": destination,
                            "scope": "calendar-settings",
                            "diffs": [f"calendar {field} differs"],
                        }
                    )
        if not any(
            i.get("scope") == "calendar-settings" and i.get("source") == cid
            for i in result["differences"]
        ):
            stats["settingsMatched"] += 1
        events = pages(
            v,
            lambda token: src.events().list(
                calendarId=cid,
                maxResults=2500,
                singleEvents=False,
                showDeleted=False,
                pageToken=token,
            ),
            "items",
        )
        stats["events"] += len(events)
        existing = {i.get("id") for i in result["notCopied"]}
        for event in events:
            key = cid + "::" + event["id"]
            if key not in events_known and key not in existing:
                result["notCopied"].append(
                    {
                        "type": "event",
                        "id": key,
                        "status": "UNMAPPED_SOURCE",
                        "error": "Live source calendar event has no migration mapping",
                    }
                )
        if acl_selected:

            def acl_check(cid=cid, destination=destination):
                source_rules = pages(
                    v,
                    lambda token: src.acl().list(calendarId=cid, pageToken=token),
                    "items",
                )
                target_rules = pages(
                    v,
                    lambda token: tgt.acl().list(
                        calendarId=destination, pageToken=token
                    ),
                    "items",
                )
                want = {key for r in source_rules if (key := _calendar_key(v, r))}
                # Target principals must not be translated a second time.
                have = {
                    (
                        r.get("scope", {}).get("type"),
                        r.get("scope", {}).get("value", "").lower(),
                        r.get("role"),
                    )
                    for r in target_rules
                    if r.get("role") != "owner"
                }
                stats["grants"] += len(want)
                stats["matchedGrants"] += len(want & have)
                for missing in sorted(want - have):
                    result["differences"].append(
                        {
                            "item": cid,
                            "source": cid,
                            "target": destination,
                            "scope": "calendar-acl",
                            "diffs": [
                                "calendar grant missing on target: "
                                + json.dumps(missing)
                            ],
                        }
                    )
                for extra in sorted(have - want):
                    result["differences"].append(
                        {
                            "item": cid,
                            "source": cid,
                            "target": destination,
                            "scope": "calendar-acl",
                            "diffs": [
                                "extra calendar grant on target: " + json.dumps(extra)
                            ],
                        }
                    )
                return check(
                    "calendar.acl." + cid,
                    "Calendar share access: " + entry.get("summary", cid),
                    "DIFFERENCES" if want != have else "MATCHED" if want else "EMPTY",
                    len(want),
                    len(want & have),
                )

            add(v, result, "calendar.acl." + cid, "Calendar share access", acl_check)
    result["sourceInventory"] = stats
    result["emptySource"] = stats["calendars"] > 0 and stats["events"] == 0
    result["checks"].append(
        check(
            "calendar.inventory",
            "All selected owned-calendar event IDs",
            "DIFFERENCES"
            if result["notCopied"]
            else "EMPTY"
            if result["emptySource"]
            else "MATCHED",
            stats["events"],
            max(
                0,
                stats["events"]
                - sum(i.get("type") == "event" for i in result["notCopied"]),
            ),
        )
    )
    result["checks"].append(
        check(
            "calendar.settings",
            "Calendar metadata and display/reminder settings",
            "DIFFERENCES"
            if any(i.get("scope") == "calendar-settings" for i in result["differences"])
            else "MATCHED",
            stats["calendars"],
            stats["settingsMatched"],
        )
    )
    result["checks"].append(
        check(
            "calendar.subscriptions",
            "Followed and shared calendars",
            "DIFFERENCES"
            if any(i.get("scope") == "subscription" for i in result["differences"])
            else "MATCHED"
            if stats["followed"]
            else "EMPTY",
            stats["followed"],
            stats["subscriptionsMatched"],
        )
    )
    if not acl_selected:
        result["checks"].append(
            check(
                "calendar.acl",
                "Calendar share access",
                "NOT_SELECTED",
                note="Calendar ACL migration was disabled; sharing equality has not been established.",
                required=False,
            )
        )


def finish(result, service):
    result["scopeVersion"] = VERSION
    result.setdefault("checks", [])
    if service == "gmail":
        result["checks"].append(
            check(
                "gmail.settings",
                "Filters, delegates, forwarding and mailbox settings",
                "NOT_VERIFIED",
                note="Message equality does not establish mailbox-setting equality.",
                required=False,
            )
        )
    if service not in ("drive", "calendar"):
        result["checks"].append(
            check(
                service + ".items",
                "Mapped items and implemented content fields",
                "INCOMPLETE"
                if result["errors"]
                else "DIFFERENCES"
                if any(
                    result[k]
                    for k in ("differences", "missing", "notCopied", "duplicates")
                )
                else "EMPTY"
                if result.get("emptySource")
                else "MATCHED"
                if result["checked"]
                else "NOT_VERIFIED",
                result["checked"],
                result["identical"],
            )
        )
    if result.get("sampled"):
        result["checks"].append(
            check(
                service + ".sample",
                "Full item coverage",
                "NOT_VERIFIED",
                note="Only a sample of mapped items was compared.",
            )
        )
    required = [c for c in result["checks"] if c.get("required", True)]
    result["coverageComplete"] = bool(required) and all(
        c["status"] in ("MATCHED", "EMPTY", "DIFFERENCES") for c in required
    )
    return result


def directory(v):
    import user_profiles

    result = v._blank()
    source = v._x(
        lambda: (
            v.auth.directory("source")
            .users()
            .get(userKey=v.src_user, projection="full", viewType="admin_view")
            .execute()
        )
    )
    target = v._x(
        lambda: (
            v.auth.directory("target")
            .users()
            .get(userKey=v.tgt_user, projection="full", viewType="admin_view")
            .execute()
        )
    )
    identities = {r["source_email"]: r["target_email"] for r in v.db.all_identities()}
    fields = user_profiles.differences(
        user_profiles.profile_body(source, v.tgt_user, identities), target
    )
    result.update(checked=1, identical=int(not fields))
    if fields:
        result["differences"].append(
            {
                "source": v.src_user,
                "target": v.tgt_user,
                "diffs": ["Directory profile field differs: " + f for f in fields],
            }
        )
    result["notes"].append(
        "Profile allow-list only; photos, aliases, custom schema and security settings are separate scopes."
    )
    return result


def chat(v):
    from link_rewrite import rewrite_text

    result = v._blank()
    src, tgt = v.auth.source_chat(v.src_user), v.auth.target_chat(v.tgt_user)
    spaces = pages(
        v, lambda token: src.spaces().list(pageSize=100, pageToken=token), "spaces"
    )
    known_spaces = {s for s, _, _ in v._pairs("chat_space")}
    known_messages = {s for s, _, _ in v._pairs("chat_message")}
    for space in spaces:
        sid = space["name"]
        if sid not in known_spaces:
            result["notCopied"].append(
                {
                    "type": "chat_space",
                    "id": sid,
                    "error": "Source Chat space has no migration mapping",
                }
            )
            continue
        messages = pages(
            v,
            lambda token: (
                src.spaces().messages().list(parent=sid, pageSize=1000, pageToken=token)
            ),
            "messages",
        )
        for message in messages:
            if message["name"] not in known_messages:
                result["notCopied"].append(
                    {
                        "type": "chat_message",
                        "id": message["name"],
                        "error": "Source Chat message has no migration mapping",
                    }
                )
    for sid, tid, _ in v._pairs("chat_message", sample=True):
        source = v._x(lambda: src.spaces().messages().get(name=sid).execute())
        target = v._x(lambda: tgt.spaces().messages().get(name=tid).execute())
        want = rewrite_text(source.get("text", ""), v.db.target_for_source_id)[0]
        result["checked"] += 1
        got = target.get("text", "")
        # chat_engine posts a bot's, or an unmapped sender's, words as the user with
        # this exact attribution: Chat lets nobody post as them. Same text, but not
        # the same sender -- named for what it is, never counted identical.
        attributed = f"[originally from {(source.get('sender') or {}).get('name', 'unknown')}] {want}"
        if want != got:
            result["differences"].append(
                {"source": sid, "target": tid, "diffs": [
                    "posted by the user with an attribution: the sender (a bot or an account "
                    "not on the target) cannot post there" if got == attributed else "Chat text differs"]}
            )
        else:
            result["identical"] += 1
    if not spaces:
        result["emptySource"] = True
    from chat_coverage import add_checks
    add_checks(v, result, src, tgt, spaces)
    return result
