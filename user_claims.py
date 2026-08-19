"""
user_claims.py
==============
Who migrates which user, when more than one machine is doing the work.

The problem
-----------
`run_batch` fans out across users inside one process on one machine. Point a
second machine at the same tenant and nothing stops both from starting the
same user -- which inserts every one of that user's messages twice, silently,
because the per-item ledger that makes a re-run idempotent is local to each
node and neither can see the other's.

So the nodes need one shared, atomic answer to "is this user mine?". That is
this module. `active_jobs` (job_admission.py) already solves the same shape of
problem for whole jobs on one box; this is the per-user, cross-box version and
uses the same mechanism -- SQLite BEGIN IMMEDIATE, no new infrastructure.

Leases
------
A claim expires unless the owning node renews it. Expiry means the node died;
it does NOT mean another node may take the user. See migrations/
004_user_claims.sql for why in full -- briefly, resume is driven by the dead
node's own local ledger, so a different node restarting that user re-inserts
everything already delivered. An expired claim is therefore:

    reclaimable by the same node    (a restart -- its ledger is intact)
    forced by a different node      (explicit, recorded, operator's call)

`force` is not a flag anyone should reach for casually: it means accepting
duplicates unless the target mailbox is cleaned first. It exists because the
alternative -- a permanently stranded user after a node dies for good -- is
worse, and because pretending the situation cannot arise is how it becomes an
outage at 2am.
"""

from __future__ import annotations

import datetime as _dt
import os
import socket

import control_plane_db as cpdb

# Long enough that an ordinary stall (a slow Drive listing, a retry storm)
# never looks like a dead node, short enough that a genuinely dead one does
# not strand its users for an hour. The renewer below refreshes at a third
# of this, so two consecutive renewals must fail before a lease lapses.
LEASE_SECONDS = 300
RENEW_EVERY = LEASE_SECONDS // 3


def node_id() -> str:
    """This machine's identity in the claims table.

    Overridable via BITPORT_NODE_ID because a hostname is not always stable
    or unique (containers, cloud images cloned from one template), and two
    nodes sharing an identity is exactly the collision this module exists to
    prevent -- it would let each silently satisfy the other's lease renewals.
    """
    return os.getenv("BITPORT_NODE_ID", "").strip() or socket.gethostname()


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _stamp(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _expiry(seconds: int = LEASE_SECONDS) -> str:
    return _stamp(_now() + _dt.timedelta(seconds=seconds))


def _local_acquire(account_id: int | None, source_user: str, *, node: str | None = None,
            services: str = "", lease_seconds: int = LEASE_SECONDS,
            force: bool = False) -> tuple[bool, str]:
    """Claim one user for this node. (claimed, reason).

    Refuses rather than waits: the caller is iterating a user list and should
    move to the next one, not block a worker on a user another node is
    already migrating.
    """
    me = node or node_id()
    now = _stamp(_now())
    with cpdb.rw() as conn:
        # BEGIN IMMEDIATE, not the default deferred transaction: two nodes
        # racing on SELECT-then-INSERT could both read "unclaimed" and both
        # insert. Taking the write lock up front serialises the whole
        # decision, the same way job_admission.try_admit does.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT node_id, status, lease_expires FROM user_claims "
            "WHERE account_id IS ? AND source_user=?",
            (account_id, source_user)).fetchone()

        if row is not None:
            owner, status, expires = row["node_id"], row["status"], row["lease_expires"]
            if status == "DONE" and not force:
                conn.rollback()
                return False, f"already migrated by {owner}"
            live = expires > now
            if live and owner != me and not force:
                conn.rollback()
                return False, f"held by {owner} until {expires}"
            if not live and owner != me and not force:
                # The lease lapsed, but this node cannot safely resume
                # another node's work -- that node's local ledger is what
                # knows which items already landed.
                conn.rollback()
                return False, (
                    f"lease from {owner} expired at {expires}; another node "
                    f"cannot resume it safely (its item ledger is local). "
                    f"Restart {owner}, or force to accept re-inserting what "
                    f"it already delivered.")
            conn.execute(
                "UPDATE user_claims SET node_id=?, status='CLAIMED', services=?, "
                "renewed_at=?, lease_expires=?, forced_from=?, detail='' "
                "WHERE account_id IS ? AND source_user=?",
                (me, services, now, _expiry(lease_seconds),
                 owner if (force and owner != me) else "",
                 account_id, source_user))
            return True, ""

        conn.execute(
            "INSERT INTO user_claims (account_id, source_user, node_id, status, "
            "services, claimed_at, renewed_at, lease_expires) "
            "VALUES (?,?,?,'CLAIMED',?,?,?,?)",
            (account_id, source_user, me, services, now, now,
             _expiry(lease_seconds)))
        return True, ""


def _local_renew(account_id: int | None, source_user: str, *, node: str | None = None,
          lease_seconds: int = LEASE_SECONDS) -> bool:
    """Push this node's lease out. False if the claim is no longer ours.

    A false return is meaningful, not cosmetic: it means something took the
    user away (an operator forced it elsewhere), and the caller should stop
    working on it rather than race the new owner.
    """
    me = node or node_id()
    with cpdb.rw() as conn:
        cur = conn.execute(
            "UPDATE user_claims SET renewed_at=?, lease_expires=? "
            "WHERE account_id IS ? AND source_user=? AND node_id=? "
            "AND status='CLAIMED'",
            (_stamp(_now()), _expiry(lease_seconds), account_id,
             source_user, me))
        return cur.rowcount > 0


def _local_finish(account_id: int | None, source_user: str, *, node: str | None = None,
           status: str = "DONE", detail: str = "") -> None:
    """Mark the user finished. Terminal, and deliberately not a delete.

    The row is the record of which node holds that user's item ledger. Delete
    it and a later verification pass has no way to know which machine to read
    the per-item history from.
    """
    me = node or node_id()
    with cpdb.rw() as conn:
        conn.execute(
            "UPDATE user_claims SET status=?, detail=?, renewed_at=? "
            "WHERE account_id IS ? AND source_user=? AND node_id=?",
            (status, detail[:400], _stamp(_now()), account_id, source_user, me))


def _local_release(account_id: int | None, source_user: str,
            *, node: str | None = None) -> None:
    """Give the user back, unstarted. For a claim taken then not worked."""
    me = node or node_id()
    with cpdb.rw() as conn:
        conn.execute(
            "DELETE FROM user_claims WHERE account_id IS ? AND source_user=? "
            "AND node_id=? AND status='CLAIMED'",
            (account_id, source_user, me))


def claims(account_id: int | None = None, *, all_accounts: bool = False) -> list[dict]:
    """Every claim, with a computed `live` flag.

    `live` is derived here rather than stored because a stored flag is wrong
    the moment the clock moves past it, and this table is read far more often
    than it is written.
    """
    now = _stamp(_now())
    sql = ("SELECT account_id, source_user, node_id, status, services, "
           "claimed_at, renewed_at, lease_expires, forced_from, detail "
           "FROM user_claims")
    args: tuple = ()
    if not all_accounts:
        sql += " WHERE account_id IS ?"
        args = (account_id,)
    sql += " ORDER BY source_user"
    with cpdb.ro() as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]
    for r in rows:
        r["live"] = r["status"] == "CLAIMED" and r["lease_expires"] > now
        r["stale"] = r["status"] == "CLAIMED" and not r["live"]
    return rows


def summary(account_id: int | None = None) -> dict:
    """Counts by node and state -- what a dashboard needs in one read."""
    rows = claims(account_id)
    by_node: dict[str, dict] = {}
    for r in rows:
        n = by_node.setdefault(r["node_id"],
                               {"node": r["node_id"], "claimed": 0, "done": 0,
                                "failed": 0, "stale": 0})
        if r["status"] == "DONE":
            n["done"] += 1
        elif r["status"] == "FAILED":
            n["failed"] += 1
        elif r["stale"]:
            n["stale"] += 1
        else:
            n["claimed"] += 1
    return {
        "nodes": sorted(by_node.values(), key=lambda x: x["node"]),
        "total": len(rows),
        "done": sum(1 for r in rows if r["status"] == "DONE"),
        "failed": sum(1 for r in rows if r["status"] == "FAILED"),
        "stale": sum(1 for r in rows if r["stale"]),
    }


# ======================================================================
# Transport
# ======================================================================
# Everything above talks to the control-plane SQLite directly. That is
# correct on the coordinator itself and impossible anywhere else: two
# machines cannot share a SQLite file over a network mount without risking
# the locking corruption that would destroy the very ledger this is meant to
# protect. So a worker node reaches the same functions over HTTP, and the
# coordinator is the only process that ever opens the database.
#
# Which mode a process is in is decided by one variable. Unset means "I am
# the coordinator" (the single-box case, and the API server itself), so an
# existing single-machine install keeps working with no configuration at all.
COORDINATOR_ENV = "BITPORT_COORDINATOR"
TOKEN_ENV = "BITPORT_NODE_TOKEN"


def coordinator_url() -> str:
    return os.getenv(COORDINATOR_ENV, "").strip().rstrip("/")


def _post(path: str, payload: dict, timeout: float = 20.0) -> dict:
    import json
    import urllib.error
    import urllib.request

    url = f"{coordinator_url()}/api/v2/claims/{path}"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Node-Token": os.getenv(TOKEN_ENV, ""),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:200]
        raise CoordinatorError(f"{exc.code} from coordinator: {detail}") from exc
    except Exception as exc:      # noqa: BLE001
        raise CoordinatorError(f"cannot reach coordinator at {url}: {exc}") from exc


class CoordinatorError(RuntimeError):
    """The coordinator could not be reached or refused the request.

    Never swallowed into "claim failed": a node that cannot reach the
    coordinator must stop, not quietly decide the user is unavailable and
    move on -- that is how a whole node sits idle through a migration while
    reporting nothing wrong.
    """


def acquire(account_id, source_user, *, node=None, services="",
            lease_seconds=LEASE_SECONDS, force=False) -> tuple[bool, str]:
    if not coordinator_url():
        return _local_acquire(account_id, source_user, node=node,
                              services=services, lease_seconds=lease_seconds,
                              force=force)
    r = _post("acquire", {"accountId": account_id, "sourceUser": source_user,
                          "nodeId": node or node_id(), "services": services,
                          "leaseSeconds": lease_seconds, "force": force})
    return bool(r.get("claimed")), str(r.get("reason") or "")


def renew(account_id, source_user, *, node=None,
          lease_seconds=LEASE_SECONDS) -> bool:
    if not coordinator_url():
        return _local_renew(account_id, source_user, node=node,
                            lease_seconds=lease_seconds)
    return bool(_post("renew", {"accountId": account_id,
                                "sourceUser": source_user,
                                "nodeId": node or node_id(),
                                "leaseSeconds": lease_seconds}).get("renewed"))


def finish(account_id, source_user, *, node=None, status="DONE",
           detail="") -> None:
    if not coordinator_url():
        _local_finish(account_id, source_user, node=node, status=status,
                      detail=detail)
        return
    _post("finish", {"accountId": account_id, "sourceUser": source_user,
                     "nodeId": node or node_id(), "status": status,
                     "detail": detail})


def release(account_id, source_user, *, node=None) -> None:
    if not coordinator_url():
        _local_release(account_id, source_user, node=node)
        return
    _post("release", {"accountId": account_id, "sourceUser": source_user,
                      "nodeId": node or node_id()})
