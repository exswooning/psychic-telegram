"""Does the ledger still describe reality?

id_mapping is authoritative: preload_mappings pulls it into memory and
get_target_id consults it before every mutating call, so anything with a
mapping is skipped on a resume. That is correct as long as the target item
it names still exists -- and nothing ever checked.

Live on 2026-08-21 it stopped being true in the worst possible way. All 200
target accounts were deleted at 17:13; fresh, empty accounts were created on
the same addresses at 17:43. Deleting a Workspace user deletes their Drive
and Gmail, so 210,456 files and 240,732 messages went with them. The ledger
still recorded every one as SUCCESS with a target id, so the tenant read as
migrated and empty at the same time, and a re-run would have skipped all of
it and reported success in seconds.

The cheap signal is the one that diagnosed it. A mapping written before its
target account existed cannot refer to anything in that account, so
comparing the account's creationTime against the user's earliest mapping
settles the whole user in ONE Directory call -- not one call per file. For
216,615 mappings that is the difference between 201 requests and 216,615.

Spot-checking files is offered as a second opinion for the case this cannot
see: an account that survived while its contents were removed some other
way. It costs a handful of calls per user and is off by default.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("ledger_verify")


@dataclass
class UserVerdict:
    source_user: str
    target_user: str
    ok: bool
    reason: str = ""
    mappings: int = 0
    account_created: str = ""
    earliest_mapping: str = ""


@dataclass
class Report:
    checked: int = 0
    stale: list = field(default_factory=list)
    unreadable: list = field(default_factory=list)

    @property
    def stale_mappings(self) -> int:
        return sum(v.mappings for v in self.stale)

    def as_text(self) -> str:
        if not self.stale and not self.unreadable:
            return f"{self.checked} user(s) checked; every mapping still resolves."
        lines = [f"{self.checked} user(s) checked, "
                 f"{len(self.stale)} with a ledger that no longer matches the "
                 f"tenant ({self.stale_mappings:,} mapping(s))."]
        for v in self.stale[:20]:
            lines.append(f"  {v.source_user} -> {v.target_user}: {v.reason}")
        if len(self.stale) > 20:
            lines.append(f"  ... and {len(self.stale) - 20} more")
        for v in self.unreadable[:10]:
            lines.append(f"  {v.source_user}: could not verify -- {v.reason}")
        return "\n".join(lines)


def _iso(value: str) -> str:
    """Directory returns 2026-08-21T17:43:46.000Z, audit_log 2026-08-21T17:43:46Z
    or with a space. Compared as text, so normalise the separators first."""
    return (value or "").replace(" ", "T").replace(".000Z", "Z").rstrip("Z")


def verify(db, directory, identities, spot_check=None) -> Report:
    """identities: iterable of (source_email, target_email).

    `spot_check(target_user, target_id) -> bool` is optional; when given it is
    asked about one mapping per otherwise-healthy user.
    """
    report = Report()
    for source_user, target_user in identities:
        report.checked += 1
        row = db.mapping_bounds(source_user)
        if not row or not row["n"]:
            continue          # nothing claimed here; see orphans() below
        verdict = UserVerdict(source_user, target_user, True,
                              mappings=row["n"],
                              earliest_mapping=_iso(row["earliest"]))
        try:
            info = directory.users().get(
                userKey=target_user, fields="primaryEmail,creationTime").execute()
        except Exception as exc:      # noqa: BLE001
            # A missing account is itself the finding, not an error: every
            # mapping for it is unreachable by definition.
            text = str(exc)
            if "404" in text or "notFound" in text or "Resource Not Found" in text:
                verdict.ok = False
                verdict.reason = ("the target account does not exist, so none "
                                  f"of its {row['n']:,} mapping(s) resolve")
                report.stale.append(verdict)
            else:
                verdict.ok = False
                verdict.reason = text[:160]
                report.unreadable.append(verdict)
            continue

        verdict.account_created = _iso(info.get("creationTime", ""))
        if (verdict.account_created and verdict.earliest_mapping
                and verdict.account_created > verdict.earliest_mapping):
            verdict.ok = False
            verdict.reason = (
                f"account created {verdict.account_created}Z but the ledger's "
                f"earliest mapping is {verdict.earliest_mapping}Z -- "
                f"{row['n']:,} mapping(s) predate the account they name, so "
                f"the items they point at were deleted with a previous account")
            report.stale.append(verdict)
            continue

        if spot_check is not None:
            sample = db.sample_mapping(source_user)
            if sample and not spot_check(target_user, sample):
                verdict.ok = False
                verdict.reason = (
                    f"a sampled item ({sample}) is gone from the target while "
                    f"the account itself predates the ledger -- "
                    f"{row['n']:,} mapping(s) are suspect")
                report.stale.append(verdict)
    return report


def orphans(db) -> list:
    """Users marked DONE whose mappings are gone but whose audit says they
    migrated something.

    verify() cannot see these. It works from mappings, and once those are
    forgotten there is nothing left for it to compare against a creation
    time -- so a user who was reopened at the mapping level but left DONE
    reads as perfectly healthy while being dropped from every dispatch.
    That is precisely what happened after the first reopen on account 7:
    24 users, the largest of them 35,490 items, skipped by a run that
    reported no problem at all.

    An empty account with no mappings is normal and is not reported. The
    audit rows are what separate "nothing to migrate" from "migrated, and
    lost the record of where it went".
    """
    return list(db.finished_but_unmapped())


def reopen_orphans(db, rows, dry_run: bool = True) -> int:
    if dry_run:
        return len(rows)
    for r in rows:
        db.reopen_identity(r["source_email"])
    return len(rows)


def reopen(db, report: Report, dry_run: bool = True) -> int:
    """Forget the stale mappings so the next run migrates them again.

    And re-open the USER, which is a separate lie in a separate table.
    Forgetting the mappings alone was not enough: identity_map.status stayed
    DONE, and main._already_done() drops DONE users before dispatch, so the
    very next run reported "dispatching 177 users" instead of 200 and
    silently skipped 24 -- including the largest, at 35,490 items. Two
    records claimed the work was finished and only one of them was corrected.

    The audit rows still stay. They are the record that this was attempted
    and when, and deleting that history would erase the evidence of what
    happened here.
    """
    total = 0
    for verdict in report.stale:
        if dry_run:
            total += verdict.mappings
            continue
        total += db.forget_mappings(verdict.source_user)
        db.reopen_identity(verdict.source_user)
    return total
