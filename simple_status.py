"""One read-only summary of every account, from LOCAL data only (GET /api/v2/simple, GET /simple).

Never a Google call: sqlite read-only connections, job_admission, the incidents table. Each account's
answer is cached in-process for TTL seconds, so any number of open tabs cost one read per account.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import control_plane_db as cpdb
import job_admission
import run_watch

TTL = 15
RATE_WINDOW_SEC = 600
DRIVE_TYPES = ("file", "folder", "shortcut")
_cache: dict[int, tuple[float, dict]] = {}
_lock = threading.Lock()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger(path: str | None) -> dict:
    out = {"users": {}, "running": [], "failed": [], "written": 0, "ratePerSec": 0.0, "verdicts": {}, "verified": 0}
    if not path or not os.path.exists(path):
        return out
    since = _iso(datetime.now(timezone.utc) - timedelta(seconds=RATE_WINDOW_SEC))
    marks = ",".join("?" * len(DRIVE_TYPES))
    with cpdb.ro(path) as c:
        out["users"] = {r[0]: r[1] for r in c.execute(
            "SELECT status, COUNT(*) FROM identity_map WHERE entity_type='user' GROUP BY status")}
        out["running"] = [r[0] for r in c.execute(
            "SELECT source_email FROM identity_map WHERE entity_type='user' AND status='RUNNING' "
            "ORDER BY source_email LIMIT 50")]
        out["failed"] = [{"user": r[0], "notes": (r[1] or "")[:200]} for r in c.execute(
            "SELECT source_email, notes FROM identity_map WHERE entity_type='user' AND status='FAILED' "
            "ORDER BY source_email LIMIT 50")]
        # ix_audit_status_type (status, item_type) answers the total without touching the table.
        out["written"] = c.execute(
            f"SELECT COUNT(*) FROM audit_log WHERE status='SUCCESS' AND item_type IN ({marks})",
            DRIVE_TYPES).fetchone()[0]
        # ponytail: audit_log has no timestamp index; this scans the SUCCESS drive rows (ms per 100k).
        recent = c.execute(
            f"SELECT COUNT(*) FROM audit_log WHERE status='SUCCESS' AND item_type IN ({marks}) AND timestamp>=?",
            (*DRIVE_TYPES, since)).fetchone()[0]
        out["ratePerSec"] = round(recent / RATE_WINDOW_SEC, 3)
        out["verdicts"] = {r[0]: r[1] for r in c.execute(
            "SELECT verdict, COUNT(*) FROM user_verification WHERE service='drive' GROUP BY verdict")}
        out["verified"] = sum(out["verdicts"].values())
    return out


def _one(acct: dict, jobs: list[dict]) -> dict:
    from account_context import db_path
    now = datetime.now(timezone.utc)
    aid = acct["id"]
    mine = []
    for j in jobs:
        if j.get("account_id") != aid:
            continue
        try:
            t0 = datetime.fromisoformat(j["started_at"].replace("Z", "+00:00"))
            elapsed = max(0, int((now - t0).total_seconds()))
        except (ValueError, KeyError, AttributeError):
            elapsed = None
        mine.append({"name": j["job_name"], "pid": j.get("pid"), "startedAt": j.get("started_at"), "elapsedSec": elapsed})
    try:
        led = _ledger(db_path(aid))
    except Exception as exc:  # noqa: BLE001 - a broken ledger must not blank the other accounts
        led = {**_ledger(None), "error": f"{type(exc).__name__}: {exc}"[:200]}
    inc = [{"at": i["last_seen_at"], "kind": i["kind"], "title": i["title"], "open": i["status"] != "resolved"}
           for i in run_watch.list_incidents(account_id=aid, limit=8)]
    return {"id": aid, "name": acct.get("name") or acct.get("email"),
            "source": acct.get("source_domain"), "target": acct.get("target_domain"),
            "jobs": mine, "incidents": inc, **led}


def summary(account_ids: set[int] | None) -> dict:
    """account_ids None = every account (a superadmin); else only those."""
    import accounts_auth
    now = time.monotonic()
    accts = [a for a in accounts_auth.list_accounts() if account_ids is None or a["id"] in account_ids]
    jobs = None
    out = []
    for a in reversed(accts):                       # oldest first
        with _lock:
            hit = _cache.get(a["id"])
        if hit and now - hit[0] < TTL:
            out.append(hit[1])
            continue
        if jobs is None:
            jobs = job_admission.list_active()
        data = _one(a, jobs)
        with _lock:
            _cache[a["id"]] = (now, data)
        out.append(data)
    return {"generatedAt": _iso(datetime.now(timezone.utc)), "accounts": out}


PAGE = """<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Bitport status</title>
<style>:root{color-scheme:light dark;--bg:#fff;--fg:#1a1a1a;--mu:#666;--bd:#ddd;--bad:#b00020}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#e8e8e8;--mu:#999;--bd:#333;--bad:#ff6b6b}}
body{margin:0;padding:12px 16px;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif;max-width:720px}
h1{font-size:18px;margin:0 0 4px}h2{font-size:16px;margin:0}section{border-top:1px solid var(--bd);padding:10px 0}
p{margin:4px 0}.mu{color:var(--mu);font-size:13px}.bad{color:var(--bad)}b{font-variant-numeric:tabular-nums}</style>
<h1>Bitport status</h1><p class=mu id=t>loading...</p><div id=a></div>
<script>
const E=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const NPT=s=>s?new Date(new Date(s).getTime()+20700000).toLocaleString("en-US",{timeZone:"UTC",month:"short",day:"numeric",hour:"numeric",minute:"2-digit",hour12:true})+" NPT":"";
const D=s=>s==null?"?":s<60?s+"s":s<3600?Math.floor(s/60)+"m":Math.floor(s/3600)+"h "+Math.floor(s%3600/60)+"m";
const KV=o=>Object.entries(o||{}).map(([k,v])=>k+" <b>"+v+"</b>").join(" · ")||"none";
async function go(){try{const r=await fetch("/api/v2/simple",{credentials:"same-origin"});
if(!r.ok)throw new Error(r.status==401||r.status==403?"sign in at /app first":"HTTP "+r.status);
const d=await r.json();t.textContent="updated "+NPT(d.generatedAt);
a.innerHTML=d.accounts.map(x=>"<section><h2>"+E(x.name)+"</h2><p class=mu>"+E(x.source)+" &rarr; "+E(x.target)+"</p>"+
"<p>Jobs: "+(x.jobs.map(j=>E(j.name)+" (pid "+j.pid+", "+D(j.elapsedSec)+")").join(", ")||"none running")+"</p>"+
"<p>Users: "+KV(x.users)+"</p>"+
(x.running.length?"<p class=mu>Running: "+x.running.map(E).join(", ")+"</p>":"")+
x.failed.map(f=>"<p class=bad>"+E(f.user)+": "+E(f.notes)+"</p>").join("")+
"<p>Drive items written: <b>"+x.written+"</b> total, <b>"+x.ratePerSec+"</b>/s over the last 10 min</p>"+
"<p>One-to-one (Drive), "+x.verified+" users: "+KV(x.verdicts)+"</p>"+
x.incidents.map(i=>"<p class=mu>"+(i.open?"<span class=bad>open</span> ":"closed ")+NPT(i.at)+" "+E(i.kind)+": "+E(i.title)+"</p>").join("")+
(x.error?"<p class=bad>"+E(x.error)+"</p>":"")+"</section>").join("")}
catch(e){t.textContent=e.message}}
go();setInterval(()=>{if(document.visibilityState=="visible")go()},30000);
document.addEventListener("visibilitychange",()=>{if(document.visibilityState=="visible")go()});
</script></html>"""
