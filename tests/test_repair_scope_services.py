"""The backlog repairs only the services the account's own runs migrated (dharan, account 7, 23 Nov 2026: after
Drive-only runs it re-verified every service and engine-copied 9,785 messages the DMS owned)."""
import verification_repairs as R

U = "u@src"


def test_nothing_recorded_means_every_service(db):
    R.ensure(db)
    assert R.allowed_services(db) is None
    assert R.services_to_check(db, U, ("drive", "gmail")) == ("drive", "gmail")


def test_a_drive_only_account_never_rechecks_or_repairs_mail(db):
    R.allow_services(db, {"drive"})
    assert R.services_to_check(db, U, ("drive", "gmail", "directory")) == ("drive", "directory")
    R.allow_services(db, ["gmail"])        # a later run that moved mail widens it
    assert R.allowed_services(db) == {"drive", "gmail"}
