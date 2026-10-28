"""Read-only Chat feature checks. Never equate attributed text with its author."""
from __future__ import annotations

import json


def add_checks(v, result, src, tgt, spaces):
    from verification_coverage import add, check, pages

    cache = {}

    def principal(person, source):
        name = person.get("name", "")
        kind = person.get("type", "")
        if not source or kind == "BOT":
            return kind, name
        if kind != "HUMAN" or not name.startswith("users/"):
            raise RuntimeError("Chat principal type cannot be mapped: " + kind)
        if name not in cache:
            original = v._x(lambda: v.auth.source_directory().users().get(
                userKey=name.split("/")[-1], projection="basic").execute())
            address = original.get("primaryEmail", "").lower()
            target_address = v.db.resolve_identity(address)
            if not target_address:
                raise RuntimeError("Chat participant has no target account mapping: " + address)
            target = v._x(lambda: v.auth.target_directory().users().get(
                userKey=target_address, projection="basic").execute())
            cache[name] = "users/" + target["id"]
        return kind, cache[name]

    def members():
        total = matched = 0
        for sid, tid, _ in v._pairs("chat_space"):
            def read(svc, space, source):
                rows = pages(v, lambda token: svc.spaces().members().list(
                    parent=space, pageSize=100, pageToken=token), "memberships")
                keys = set()
                for row in rows:
                    if row.get("groupMember"):
                        raise RuntimeError("Chat group membership requires explicit group identity comparison")
                    keys.add((*principal(row.get("member") or {}, source),
                              row.get("role"), row.get("state")))
                return keys
            want, have = read(src, sid, True), read(tgt, tid, False)
            total += len(want)
            matched += len(want & have)
            if want != have:
                want_people = {(kind, name): (role, state) for kind, name, role, state in want}
                have_people = {(kind, name): (role, state) for kind, name, role, state in have}
                missing = want_people.keys() - have_people.keys()
                extra = have_people.keys() - want_people.keys()
                changed = sum(want_people[p] != have_people[p]
                              for p in want_people.keys() & have_people.keys())
                result["differences"].append({"source": sid, "target": tid, "scope": "chat-members",
                    "diffs": [f"Chat participants differ: {len(missing)} missing "
                              f"({sum(kind == 'BOT' for kind, _ in missing)} app/bot), "
                              f"{len(extra)} extra; {changed} existing participant role/state difference(s)"]})
        return check("chat.members", "Chat membership and roles",
                     "DIFFERENCES" if matched != total or any(i.get("scope") == "chat-members" for i in result["differences"])
                     else "MATCHED" if total else "EMPTY", total, matched)

    def space_types():
        total = matched = 0
        for sid, tid, _ in v._pairs("chat_space"):
            s = v._x(lambda: src.spaces().get(name=sid).execute())
            t = v._x(lambda: tgt.spaces().get(name=tid).execute())
            total += 1
            if s.get("spaceType") == t.get("spaceType"):
                matched += 1
            else:
                result["differences"].append({"source": sid, "target": tid, "scope": "chat-space-type",
                    "diffs": [f"Chat conversation type differs: {s.get('spaceType')} -> {t.get('spaceType')}; "
                              "an imported group chat does not reproduce a direct-message conversation"]})
        return check("chat.spaces", "Chat conversation types",
                     "DIFFERENCES" if matched != total else "MATCHED" if total else "EMPTY", total, matched)

    messages = None

    def message_pairs():
        nonlocal messages
        if messages is None:
            pending = []
            for sid, tid, _ in v._pairs("chat_message", sample=True):
                s = v._x(lambda: src.spaces().messages().get(name=sid).execute())
                t = v._x(lambda: tgt.spaces().messages().get(name=tid).execute())
                pending.append((sid, tid, s, t))
            messages = pending
        return messages

    def threads():
        total = matched = 0
        for sid, tid, s, t in message_pairs():
            total += 1
            diffs = []
            if principal(s.get("sender") or {}, True) != principal(t.get("sender") or {}, False):
                diffs.append("Original Chat sender identity was not reproduced")
            source_thread = (s.get("thread") or {}).get("name")
            target_thread = (t.get("thread") or {}).get("name")
            mapped = v.db.get_target_id(v.src_user, source_thread, "chat_thread") if source_thread else None
            if source_thread and (not mapped or target_thread != mapped):
                diffs.append("Chat thread identity differs or lacks a mapping")
            if s.get("createTime") != t.get("createTime"):
                diffs.append("Original Chat message timestamp was not preserved")
            if diffs:
                result["differences"].append({"source": sid, "target": tid, "scope": "chat-thread", "diffs": diffs})
            else:
                matched += 1
        return check("chat.threads", "Chat authors, timestamps and thread structure",
                     "DIFFERENCES" if matched != total else "MATCHED" if total else "EMPTY", total, matched)

    def attachments():
        total = 0
        for _, _, s, t in message_pairs():
            total += sum(len(m.get(k) or []) for m in (s, t) for k in ("attachment", "cards", "cardsV2"))
        return check("chat.attachments", "Chat attachments and structured app payloads",
                     "NOT_VERIFIED" if total else "EMPTY", total, 0,
                     "Binary attachment bytes and app card behavior require separate qualification." if total else
                     "No attachment or card payload found on either side of the compared messages.")

    def reactions():
        total = matched = 0
        for sid, tid, _, _ in message_pairs():
            def read(svc, mid, source):
                rows = pages(v, lambda token: svc.spaces().messages().reactions().list(
                    parent=mid, pageSize=200, pageToken=token), "reactions")
                return {(*principal(row.get("user") or {}, source),
                         json.dumps(row.get("emoji") or {}, sort_keys=True)) for row in rows}
            want, have = read(src, sid, True), read(tgt, tid, False)
            total += len(want)
            matched += len(want & have)
            if want != have:
                result["differences"].append({"source": sid, "target": tid, "scope": "chat-reactions",
                    "diffs": [f"Chat reactions differ: {len(want-have)} missing, {len(have-want)} extra"]})
        return check("chat.reactions", "Chat reactions and reacting identities",
                     "DIFFERENCES" if matched != total or any(i.get("scope") == "chat-reactions" for i in result["differences"])
                     else "MATCHED" if total else "EMPTY", total, matched)

    for key, label, fn in (("spaces", "Chat conversation types", space_types),
                           ("members", "Chat membership and roles", members),
                           ("attachments", "Chat attachments", attachments),
                           ("reactions", "Chat reactions", reactions),
                           ("threads", "Chat authors and thread structure", threads)):
        add(v, result, "chat." + key, label, fn)
