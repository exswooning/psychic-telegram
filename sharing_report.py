"""Every sharing grant the migration made, tenant-wide: who got access, inside or outside the
organisation, and to which file. Read from the account's ledger only -- no Google call.

A grant is one `audit_log` row (item_type 'acl'), keyed `<source file id>:<grantee>`:
  <email>             a person or group   internal when its domain is the source's or the target's
  anyone              anyone with the link
  domain:<domain>     everyone in a domain  internal when it is the organisation's own
Its role is in the row's detail (`role=writer`), recorded from 9 Nov 2026 on; older rows say None.
"""
from __future__ import annotations

import collections
import csv
import io

KINDS = ("external", "public", "internal", "domain")
LABEL = {"external": "External people", "public": "Anyone with the link",
         "internal": "Inside the organisation", "domain": "A whole outside domain"}
LINK = "https://drive.google.com/open?id={}"


def classify(grantee: str, own: set) -> tuple[str, str]:
    """(kind, who) for one grant's grantee part. `own` holds the organisation's domains."""
    g = (grantee or "").strip().lower()
    if g == "anyone":
        return "public", "anyone with the link"
    if g.startswith("domain:"):
        d = g[7:]
        return ("internal" if d in own else "domain"), d
    if "@" in g:
        return ("internal" if g.rsplit("@", 1)[1] in own else "external"), g
    return "internal" if g in own else "domain", g      # a bare domain from an older key form


def _rows(conn, own: set):
    names = {}
    for sid, tid, name in conn.execute(
            "SELECT source_id, target_id, source_name FROM id_mapping WHERE type IN ('file','folder')"):
        names[sid] = (tid, name)
    for owner, item, status, detail in conn.execute(
            "SELECT source_user, item_id, status, error_message FROM audit_log WHERE item_type='acl'"):
        from ledger_keys import parse_grant
        g = parse_grant(item)
        if g.kind in ("no-email", "list-failed") or not g.file_id:      # markers, not grants
            continue
        fid = g.file_id
        kind, who = classify("anyone" if g.kind == "anyone" else (f"domain:{g.who}" if g.kind == "domain" else g.who), own)
        tid, name = names.get(fid, (None, None))
        role = (detail or "")[5:] if (detail or "").startswith("role=") else None
        yield {"kind": kind, "grantee": who, "owner": owner, "file": name or "(not mapped)",
               "sourceId": fid, "targetId": tid, "role": role, "status": status,
               "sourceUrl": LINK.format(fid), "targetUrl": LINK.format(tid) if tid else None}


def report(conn, own_domains, kind: str | None = None, q: str = "", limit: int = 200,
           offset: int = 0, status: str = "SUCCESS") -> dict:
    """Totals by kind, the outside people and domains ranked by files, and one page of grants."""
    own = {d.lower() for d in own_domains if d}
    totals = {k: {"label": LABEL[k], "grants": 0, "files": set()} for k in KINDS}
    others: collections.Counter = collections.Counter()
    people: dict = collections.defaultdict(set)
    domains: dict = collections.defaultdict(set)
    roles: collections.Counter = collections.Counter()
    page, matched, q = [], 0, (q or "").strip().lower()
    for r in _rows(conn, own):
        if r["status"] != status:
            others[r["status"]] += 1
            continue
        t = totals[r["kind"]]
        t["grants"] += 1
        t["files"].add(r["sourceId"])
        roles[r["role"] or "not recorded"] += 1
        if r["kind"] == "external":
            people[r["grantee"]].add(r["sourceId"])
            domains[r["grantee"].rsplit("@", 1)[1]].add(r["sourceId"])
        elif r["kind"] == "domain":
            domains[r["grantee"]].add(r["sourceId"])
        if (kind and r["kind"] != kind) or (q and q not in r["grantee"] and q not in r["file"].lower()
                                             and q not in r["owner"].lower()):
            continue
        matched += 1
        if offset < matched <= offset + limit:
            page.append(r)
    rank = lambda m, n: [{"name": k, "files": len(v)} for k, v in          # noqa: E731
                         sorted(m.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:n]]
    return {"totals": {k: {"label": v["label"], "grants": v["grants"], "files": len(v["files"])}
                       for k, v in totals.items()},
            "roles": dict(roles), "notGranted": dict(others),
            "externalPeople": rank(people, 50), "externalDomains": rank(domains, 25),
            "matched": matched, "offset": offset, "limit": limit, "grants": page}


def to_csv(conn, own_domains, kind: str | None = None, q: str = "") -> str:
    data = report(conn, own_domains, kind=kind, q=q, limit=10**9)
    out = io.StringIO()
    cols = ("kind", "grantee", "role", "owner", "file", "sourceUrl", "targetUrl")
    w = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in data["grants"]:
        w.writerow(r)
    return out.getvalue()
