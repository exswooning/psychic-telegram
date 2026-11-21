"""fableplan2 P4.2-P4.4: seed one planned user (profile_plan.py) into a tenant. `s` is the seed_sandbox module, passed in
(as fixtures.py does) so its helpers are the ones in use and a script run as __main__ does not load it twice.

Drive: a pool of unique files is uploaded once by one user (the only bytes that cross this box); every user's files are
files.copy of a pool file inside the tenant, named, timed and placed by the plan. Mail goes up message by message
(Gmail has no server-side copy) in chunks.

A killed seed started again adds nothing twice. Every Drive item carries appProperties {bpseed, bpi: its node id} and a
rerun skips the ids it finds; the root folder's appProperties hold the services that finished (`done`) and how many
chunks of mail are in (`mail_chunks`); a message carries a Message-ID made from its position, and the one chunk a kill
can leave half-done is looked up by that id before it is sent again (Gmail search can lag a few seconds behind an insert:
a kill and a restart inside that gap can leave a duplicate)."""
from __future__ import annotations

import bisect
import math
import os
import re
import time
import zlib
from email.header import Header

import profile_plan as pp

ROOT = "MIGRATION-TEST"            # the seeder's own root: reset and the existing "already seeded" check know it
POOL = "BITPORT-SEED-POOL"
CHUNK = 100                        # messages sent together, and at most what a kill can leave half-done
OWN_LINKS = 20                     # own file ids kept for link mail


def _mark(**extra) -> dict:
    return {"bpseed": "1", **{k: str(v) for k, v in extra.items()}}


def _call(retry, fn):
    return retry(fn)()


def _list(drive, retry, q: str, fields: str) -> list:
    out, token = [], None
    while True:
        res = _call(retry, lambda t=token: drive.files().list(q=q, pageSize=1000, pageToken=t, spaces="drive",
                                                               fields=f"nextPageToken,files({fields})").execute())
        out += res.get("files") or []
        token = res.get("nextPageToken")
        if not token:
            return out


def _root(drive, retry, create: bool = True) -> dict | None:
    q = f"'root' in parents and 'me' in owners and trashed = false and name = '{ROOT}'"
    for f in _list(drive, retry, q, "id,name,appProperties"):
        if f.get("name") == ROOT:
            return f
    if not create:
        return None
    body = {"name": ROOT, "mimeType": pp.FOLDER, "parents": ["root"], "appProperties": _mark(bpi="root")}
    return {"id": _call(retry, lambda: drive.files().create(body=body, fields="id").execute())["id"], "appProperties": body["appProperties"]}


def _set(drive, retry, root: dict, **props) -> None:
    _call(retry, lambda: drive.files().update(fileId=root["id"], body={"appProperties": {k: str(v) for k, v in props.items()}},
                                              fields="id").execute())
    root.setdefault("appProperties", {}).update({k: str(v) for k, v in props.items()})


def done_services(drive, retry) -> set:
    root = _root(drive, retry, create=False)
    return set(filter(None, ((root or {}).get("appProperties") or {}).get("done", "").split(",")))


def mark_done(drive, retry, service: str) -> None:
    root = _root(drive, retry)
    have = set(filter(None, (root.get("appProperties") or {}).get("done", "").split(",")))
    _set(drive, retry, root, done=",".join(sorted(have | {service})))


# ---- the pool -------------------------------------------------------------------------------------------------
def _blob(label: str, size: int) -> bytes:
    head = f"BITPORT-POOL {label}\n".encode()
    return (head + b"0123456789abcdef" * (size // 16 + 1))[:max(size, 0)]


def build_pool(s, settings, user: str, mix: dict, pool_bytes: int, largest_cap: int, share_domain: bool = True) -> dict:
    """Upload the pool once, as `user`. Safe to run again: a file already there (by name) is reused."""
    drive, _, _ = s.build_services(settings, user)
    retry = s._retry_factory(settings, limiter=s._seed_drive_limiter(settings))
    sizes = pp.pool_sizes(mix, pool_bytes, largest_cap)
    folder = None
    for f in _list(drive, retry, f"'root' in parents and 'me' in owners and trashed = false and name = '{POOL}'", "id,name"):
        if f.get("name") == POOL:
            folder = f["id"]
    if folder is None:
        folder = _call(retry, lambda: drive.files().create(body={"name": POOL, "mimeType": pp.FOLDER, "parents": ["root"]},
                                                           fields="id").execute())["id"]
        if share_domain:       # every other user copies from it as themselves
            _call(retry, lambda: drive.permissions().create(
                fileId=folder, body={"type": "domain", "role": "reader", "domain": settings.source_domain,
                                     "allowFileDiscovery": False}, sendNotificationEmail=False, fields="id").execute())
    have = {f["name"]: f["id"] for f in _list(drive, retry, f"'{folder}' in parents and trashed = false", "id,name")}
    pool: dict = {"folder": folder, "files": {}, "natives": {}, "ids": [], "bytes": 0, "uploaded": 0, "largest": 0}

    def put(nm, mime, data=None, native=False):
        fid = have.get(nm)
        if fid is None:
            body = {"name": nm, "parents": [folder]}
            if native:
                body["mimeType"] = mime
            media = s._media(data, "text/plain" if native else mime) if data is not None else None
            fid = _call(retry, lambda: drive.files().create(body=body, media_body=media, fields="id").execute())["id"]
            pool["uploaded"] += len(data or b"")
        pool["ids"].append(fid)
        return fid

    for mime, classes in sorted(sizes.items()):
        slug = f"{zlib.crc32(mime.encode()):08x}"
        for size in classes:
            nm = f"pool-{slug}-{size}"
            pool["files"].setdefault(mime, []).append((size, put(nm, mime, _blob(nm, size))))
            pool["bytes"] += size
            pool["largest"] = max(pool["largest"], size)
    for mime in pp.NATIVES:
        text = b"Name,Value\nrow,1\n" if mime.endswith("spreadsheet") else b"Pool document.\n"
        pool["natives"][mime] = put("pool-" + mime.rsplit(".", 1)[1], mime, None if mime.endswith("presentation") else text, native=True)
    return pool


def pool_file_count(mix: dict, pool_bytes: int, largest_cap: int) -> int:
    return sum(len(v) for v in pp.pool_sizes(mix, pool_bytes, largest_cap).values()) + len(pp.NATIVES)


def probe_upload(s, settings, user: str, seconds: float, chunk: int = 4 * 1024 * 1024) -> float:
    """Bytes per second this box sends to Drive, measured by uploading random chunks for `seconds` (at least one) and
    deleting them again."""
    drive, _, _ = s.build_services(settings, user)
    retry = s._retry_factory(settings)
    sent, made, t0 = 0, [], time.time()
    while True:
        made.append(_call(retry, lambda: drive.files().create(
            body={"name": f"bitport-upload-probe-{len(made)}"}, media_body=s._media(os.urandom(chunk), "application/octet-stream"),
            fields="id").execute())["id"])
        sent += chunk
        if time.time() - t0 >= seconds:
            break
    rate = sent / max(time.time() - t0, 1e-6)
    for fid in made:
        try:
            _call(retry, lambda f=fid: drive.files().delete(fileId=f).execute())
        except Exception:      # noqa: BLE001 - a probe file left behind is harmless
            pass
    return rate


# ---- Drive ----------------------------------------------------------------------------------------------------
def _workers() -> int:
    n = int(os.getenv("SEED_LEAF_WORKERS", "0"))
    if n > 0:
        return n
    try:
        import resources
        return resources.seed_leaf_workers()
    except Exception:      # noqa: BLE001 - sizing must never break a seed
        return 4


def _pick(pool: dict, mime: str, size: int):
    if mime in pp.NATIVES:
        return pool["natives"].get(mime)
    classes = pool["files"].get(mime)
    if not classes:
        return None
    fit = [c for c in classes if c[0] <= size]
    return (fit[-1] if fit else classes[0])[1]


def _existing(drive, retry) -> dict:
    out = {}
    for f in _list(drive, retry, "'me' in owners and trashed = false and appProperties has { key='bpseed' and value='1' }",
                   "id,appProperties"):
        props = f.get("appProperties") or {}
        if props.get("bpseed") == "1" and props.get("bpi", "").isdigit():
            out[int(props["bpi"])] = f["id"]
    return out


def build_drive(s, drive, user: str, p: dict, pool: dict, retry) -> dict:
    import threading
    root = _root(drive, retry)
    have = _existing(drive, retry)
    ids, lock = {}, threading.Lock()
    m = {"folders": 0, "docs": 0, "sheets": 0, "slides": 0, "binaries": 0, "shortcuts": 0, "created": 0, "skipped": 0,
         "bytes": 0, "total_files": 0, "items": {"root": root["id"]}}
    kind_key = {pp.NATIVES[0]: "docs", pp.NATIVES[1]: "sheets", pp.NATIVES[2]: "slides"}

    def parent_of(n):
        return root["id"] if n["parent"] < 0 else ids.get(n["parent"], root["id"])

    def body(n, **extra):
        return {"name": n["name"], "parents": [parent_of(n)], "createdTime": s._iso(n["created_days"]),
                "modifiedTime": s._iso(n["modified_days"]), "appProperties": _mark(bpi=n["i"]), **extra}

    def copy_file(n):
        if n["i"] in have:
            ids[n["i"]] = have[n["i"]]
            with lock:
                m["skipped"] += 1
            return
        src = _pick(pool, n["mime"], n["size"])
        if src is None:
            with lock:
                m["skipped"] += 1
            return
        fid = _call(retry, lambda: drive.files().copy(fileId=src, body=body(n), fields="id", supportsAllDrives=True).execute())["id"]
        with lock:
            ids[n["i"]] = fid
            m["created"] += 1
            m[kind_key.get(n["mime"], "binaries")] += 1
            m["bytes"] += n["size"]

    window, batch = max(4, _workers() * 4), []
    for n in pp.tree(p):
        if n["kind"] == "folder":
            if n["i"] in have:
                ids[n["i"]] = have[n["i"]]
            else:
                ids[n["i"]] = _call(retry, lambda: drive.files().create(body=body(n, mimeType=pp.FOLDER), fields="id").execute())["id"]
                m["created"] += 1
            m["folders"] += 1
        elif n["kind"] == "file":
            batch.append(n)
            if len(batch) >= window:
                s._run_parallel(batch, copy_file, _workers())
                batch = []
        else:
            if batch:
                s._run_parallel(batch, copy_file, _workers())
                batch = []
            if n["i"] in have or n["target"] not in ids:
                m["skipped"] += 1
                continue
            _call(retry, lambda: drive.files().create(body=body(n, mimeType=pp.SHORTCUT,
                                                               shortcutDetails={"targetId": ids[n["target"]]}), fields="id").execute())
            m["created"] += 1
            m["shortcuts"] += 1
    if batch:
        s._run_parallel(batch, copy_file, _workers())
    m["total_files"] = sum(m[k] for k in ("docs", "sheets", "slides", "binaries", "shortcuts"))
    m["folders_ids"] = [v for k, v in sorted(ids.items()) if k < p["folders"]]
    return m


def link_items(s, drive, p: dict, pool: dict, retry) -> dict:
    """The ids Drive-link mail points at: this user's own files and a folder, one file that is deleted, and the pool's
    files (owned by the pool user: a colleague's, or a user outside the run's). A user who has not been through
    build_drive yet has no own files, and those links fall back to the pool's."""
    items = {"pool": list(pool["ids"]) if pool else []}
    root = _root(drive, retry, create=False)
    if root:
        mine = sorted(_existing(drive, retry).items())
        files = [fid for i, fid in mine if i >= p["folders"]]
        items["own"] = files[:OWN_LINKS]
        items["folder"] = ([fid for i, fid in mine if i < p["folders"]] or [root["id"]])[0]
        if p["link_kinds"].get("deleted") and pool and pool["ids"]:
            gone = _call(retry, lambda: drive.files().copy(fileId=pool["ids"][0], body={"name": "removed", "parents": [root["id"]]},
                                                           fields="id", supportsAllDrives=True).execute())["id"]
            _call(retry, lambda: drive.files().delete(fileId=gone).execute())
            items["deleted"] = gone
    return items


# ---- mail -----------------------------------------------------------------------------------------------------
def _label_names(p: dict) -> list[str]:
    rng = pp._rng(p["key"], "labels")
    return [re.sub(r"[^\w ]", "", pp.name(rng))[:40].strip() or f"label {k}" for k in range(p["labels"])]


def seed_mail(s, gmail, drive, user: str, peers: list[str], external: str, p: dict, items: dict, msg_bytes: int, retry) -> dict:
    import threading
    total = p["messages"]
    m = {"messages": 0, "unread": 0, "starred": 0, "in_spam": 0, "in_trash": 0, "with_attachment": 0, "labels": [],
         "with_drive_link": 0, "link_fallbacks": 0, "found_present": 0, "failed": 0, "planned": total, "drafts": 0}
    if not total:
        return m
    key = p["key"]
    labels = {}
    existing = {lb["name"]: lb["id"] for lb in _call(retry, lambda: gmail.users().labels().list(userId="me").execute()).get("labels", [])
                if lb.get("name")}
    for nm in _label_names(p):
        lid = existing.get(nm)
        if lid is None:
            try:
                lid = _call(retry, lambda n=nm: gmail.users().labels().create(userId="me", body={
                    "name": n, "labelListVisibility": "labelShow", "messageListVisibility": "show"}).execute())["id"]
            except Exception as exc:      # noqa: BLE001
                print(f"  ! label: {str(exc)[:100]}")
                continue
        labels[nm] = lid
        m["labels"].append(nm)

    roots = sorted({0} | set(pp._rng(key, "roots").sample(range(1, total), min(p["threads"], total) - 1))) if total > 1 else [0]
    root_set = set(roots)
    link_rng = pp._rng(key, "links")
    kinds = _shuffled_kinds(p, link_rng)
    link_at = dict(zip(link_rng.sample(range(total), len(kinds)), kinds))
    senders = list(peers) + [external]
    sizes_mean = max(msg_bytes, pp.MIN_MSG_BYTES)
    lock = threading.Lock()

    def mid(i):
        return f"bp{key:08x}-{i}"

    def build(i):
        rng = pp._rng(key, "msg", i)
        subj = rng.choice(s.SUBJECTS).format(q=rng.randint(1, 4), y=2020 + rng.randint(0, 6), p=rng.choice(["Apollo", "Borealis", "Cygnus"]),
                                            a=rng.choice(["Acme Corp", "Globex", "Initech"]), n=rng.randint(100, 999))
        if rng.random() < pp.DEVANAGARI / 2:
            subj = pp.name(rng)
        reply_to = ""
        if i not in root_set:
            reply_to = mid(roots[bisect.bisect_right(roots, int(i * rng.random() ** 2)) - 1])
            subj = "Re: " + subj
        subj = subj if subj.isascii() else Header(subj, "utf-8").encode()
        sender = rng.choice(senders)
        lab = ["INBOX"]
        r = rng.random()
        if r < 0.30:
            lab.append("UNREAD")
        if r > 0.88:
            lab.append("STARRED")
        if labels and rng.random() < 0.25:
            lab.append(rng.choice(sorted(labels.values())))
        if rng.random() < 0.04:
            lab = ["SPAM"]
        elif rng.random() < 0.04:
            lab = ["TRASH"]
        elif rng.random() < 0.12:
            lab = ["SENT"]
        target = max(pp.MIN_MSG_BYTES, int(rng.expovariate(1 / sizes_mean)))
        body, att, linked = "", 0, i in link_at
        if linked:
            kind = link_at[i]
            fid = _link_id(items, kind, rng)
            if fid is None:
                fid, kind = _link_id(items, "colleague", rng), "colleague"
                with lock:
                    m["link_fallbacks"] += 1
            if fid is not None:
                body = s._linked_body(fid, 3 if kind == "folder" else rng.randint(0, 2))
            else:
                linked = False
        if target > 8192:
            att = (target * 3 // 4) // 1024
        else:
            body = (body + "\r\n" + "Recorded for audit. " * (target // 20))[:max(target, len(body))]
        raw = s._rfc822(subj, sender, user, int(rng.random() ** 1.3 * 3650) + 1, body=body, msg_id=mid(i), in_reply_to=reply_to,
                        attachment_kb=att)
        return raw, lab, att, linked, i

    def send(pl):
        raw, lab, att, linked, i = pl
        try:
            s.insert_message(gmail, retry, raw, lab)
        except Exception as exc:      # noqa: BLE001
            print(f"  ! message {i}: {str(exc)[:100]}")
            with lock:
                m["failed"] += 1
            return
        with lock:
            m["messages"] += 1
            m["unread"] += "UNREAD" in lab
            m["starred"] += "STARRED" in lab
            m["in_spam"] += "SPAM" in lab
            m["in_trash"] += "TRASH" in lab
            m["with_attachment"] += bool(att)
            m["with_drive_link"] += bool(linked)

    def present(i) -> bool:
        res = _call(retry, lambda: gmail.users().messages().list(userId="me", q=f"rfc822msgid:{mid(i)}@seed.test",
                                                                 includeSpamTrash=True, maxResults=1).execute())
        return bool(res.get("messages"))

    root = _root(drive, retry)
    first = (root.get("appProperties") or {}).get("mail_chunks")
    first = int(first) if first is not None else None
    chunks = math.ceil(total / CHUNK)
    if first is None:
        _set(drive, retry, root, mail_chunks=0)
    for c in range(first or 0, chunks):
        idx = range(c * CHUNK, min(total, (c + 1) * CHUNK))
        if first is not None and c == first:        # the chunk a kill can have left half-done
            keep = []
            for i in idx:
                if present(i):
                    m["found_present"] += 1
                else:
                    keep.append(i)
            idx = keep
        before = m["failed"]
        s._run_parallel([build(i) for i in idx], send, s.MAIL_CAL_WORKERS)
        if m["failed"] > before:      # stop here: the rerun finishes this chunk, and nothing after it was sent
            break
        _set(drive, retry, root, mail_chunks=c + 1)
    return m


def _shuffled_kinds(p: dict, rng) -> list[str]:
    kinds = [k for k, n in sorted(p["link_kinds"].items()) for _ in range(n)]
    rng.shuffle(kinds)
    return kinds


def _link_id(items: dict, kind: str, rng):
    if kind == "own":
        pool = items.get("own")
    elif kind == "folder":
        pool = [items["folder"]] if items.get("folder") else None
    elif kind == "deleted":
        pool = [items["deleted"]] if items.get("deleted") else None
    else:      # a colleague's and a user outside the run's file are both a pool file (owned by the pool user)
        pool = items.get("pool")
    return rng.choice(pool) if pool else None
