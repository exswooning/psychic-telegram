"""When to stop waiting for an offline node and give its user to another.

The trade-off, stated plainly. A node holding a user goes offline. Its
LOCAL ledger is what makes resuming that user cheap -- gmail_engine skips a
message when db.get_target_id() finds a row, and those rows are on the
machine that died. So:

  * WAIT and the node comes back: it resumes from its own ledger and
    finishes the remaining part. Cheap.
  * REASSIGN: the new node starts from an empty ledger and re-delivers
    everything already done. For Gmail that is slow but safe -- the target
    is asked for the Message-ID. For Drive it DUPLICATES FILES, because the
    duplicate check reads the local id_mapping and cannot see the other
    machine's work. See 004_user_claims.sql, which spells this out, and
    MULTINODE.md.

So the question is not "is the node dead" -- that is unknowable from here,
which is exactly why this was left as a manual force until now. The
question is "has waiting stopped being worth it".

The policy: WAIT UP TO THE COST OF REDOING THE WORK, THEN REASSIGN.

That bound is the whole idea, and it is not arbitrary. If the node returns
within the redo cost, waiting was strictly better. If it has not returned
by then, you have already spent the redo cost waiting AND you still have to
pay it -- so every further second of waiting is a second you cannot get
back, whether the node returns or not. Waiting longer can only lose. It is
the same shape as the classic wait-or-restart problem.

The cost of redoing is not one number, and that is what makes the policy do
the right thing per service without a second rule: Drive's redo re-copies
everything and leaves duplicates, so its cost is high and the wait is long;
Gmail's redo deduplicates against the target, so its cost is low and the
wait is short. One formula, and it reproduces MULTINODE.md's advice on its
own.
"""
from __future__ import annotations

import time

import control_plane_db as cpdb

# What one user costs to redo from scratch, when nothing has been measured
# yet. A first run has no history to learn from, and refusing to decide is
# worse than deciding from a defensible default.
DEFAULT_USER_SECONDS = 20 * 60

# How much more expensive a redo is than the original run, per service.
#
# 1.0 means "no worse than doing it the first time". Gmail is close to that:
# _find_by_message_id asks the TARGET whether a message is already there, so
# a redo mostly re-reads and re-checks rather than re-inserting.
#
# Drive is 4x, and the multiplier is standing in for harm rather than time.
# A redo re-copies every file AND leaves the first copy behind as a
# duplicate, which someone then has to find and remove. Making it expensive
# is what makes this policy wait a long time before duplicating a user's
# Drive -- which is the advice MULTINODE.md already gives operators, arrived
# at here from the cost instead of from a separate rule.
REDO_MULTIPLIER = {
    "gmail": 1.0,
    "calendar": 1.2,
    "contacts": 1.2,
    "tasks": 1.2,
    "chat": 1.5,
    "drive": 4.0,
}
DEFAULT_MULTIPLIER = 2.0

# Never reassign before this, however cheap the work looks. A node restarting
# after a deploy, or a laptop waking from sleep, is back within a couple of
# minutes and resumes for free -- reassigning inside that window pays the
# whole redo cost to save nothing.
MIN_WAIT_S = 180


def _seconds_between(a: str, b: str) -> float | None:
    fmt = "%Y-%m-%dT%H:%M:%S"
    try:
        ta = time.mktime(time.strptime(a[:19], fmt))
        tb = time.mktime(time.strptime(b[:19], fmt))
    except (ValueError, TypeError):
        return None
    return max(0.0, tb - ta)


def observed_user_seconds(account_id: int | None,
                          default: float = DEFAULT_USER_SECONDS) -> float:
    """How long a user has actually been taking on this account.

    Median, not mean: one user with a 40 GB mailbox should not drag the
    estimate for the other 199, and a stuck run that was eventually killed
    should not either.
    """
    with cpdb.ro() as conn:
        rows = conn.execute(
            "SELECT claimed_at, renewed_at FROM user_claims "
            "WHERE status='DONE' AND (account_id IS ? OR ? IS NULL) "
            "ORDER BY rowid DESC LIMIT 200",
            (account_id, account_id)).fetchall()
    spans = [s for s in (_seconds_between(r["claimed_at"], r["renewed_at"])
                         for r in rows) if s and s > 0]
    if not spans:
        return default
    spans.sort()
    return spans[len(spans) // 2]


def redo_cost_s(services: str, account_id: int | None = None,
                observed: float | None = None) -> float:
    """What it would cost to hand this user to a different machine.

    The worst multiplier among the services in play, not the average: a run
    covering Gmail and Drive together is as bad as its Drive half, because
    that is the half that duplicates.
    """
    base = observed if observed is not None else observed_user_seconds(account_id)
    names = [s.strip().lower() for s in (services or "").split(",") if s.strip()]
    if not names or "all" in names:
        # "all" means everything the tenant has, which includes Drive.
        mult = max(REDO_MULTIPLIER.values())
    else:
        mult = max((REDO_MULTIPLIER.get(n, DEFAULT_MULTIPLIER) for n in names),
                   default=DEFAULT_MULTIPLIER)
    return base * mult


def decide(stale_for_s: float, services: str, account_id: int | None = None,
           observed: float | None = None) -> dict:
    """Wait, or hand this user to another machine?

    Returns the decision AND the numbers behind it. The numbers are the
    point: this quietly duplicates somebody's Drive files if it is wrong,
    so a claim that gets reassigned should be able to say why in terms an
    operator can check.
    """
    cost = redo_cost_s(services, account_id, observed)
    deadline = max(MIN_WAIT_S, cost)
    remaining = max(0.0, deadline - stale_for_s)
    reassign = stale_for_s >= deadline
    if reassign:
        why = (f"offline {stale_for_s / 60:.0f}m, longer than the "
               f"{deadline / 60:.0f}m it would take to redo this user "
               f"({services or 'all services'}) -- waiting can no longer win")
    else:
        why = (f"offline {stale_for_s / 60:.0f}m; waiting up to "
               f"{deadline / 60:.0f}m, because redoing "
               f"{services or 'all services'} for this user costs about that "
               f"and a resume from its own ledger costs nearly nothing")
    return {"reassign": reassign, "waitDeadlineS": deadline,
            "redoCostS": cost, "remainingS": remaining, "reason": why}
