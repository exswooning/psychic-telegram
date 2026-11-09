"""The Sharing view: every grant the migration made, split inside / outside / public, with roles."""
import sharing_report as S
from tests.conftest import SRC_USER

OWN = ("tenanta.com", "tenantb.com")


def _grants(db):
    db.record_mapping(SRC_USER, "f1", "t1", "file", source_name="Budget.xlsx")
    db.record_mapping(SRC_USER, "f2", "t2", "file", source_name="Minutes.docx")
    db.log_audit(SRC_USER, "f1:friend@gmail.com", "acl", "SUCCESS", "role=writer")
    db.log_audit(SRC_USER, "f2:friend@gmail.com", "acl", "SUCCESS", "role=reader")
    db.log_audit(SRC_USER, "f1:anyone", "acl", "SUCCESS", "role=reader")
    db.log_audit(SRC_USER, "f1:bob@tenantb.com", "acl", "SUCCESS")              # before roles were kept
    db.log_audit(SRC_USER, "f2:domain:tenantb.com", "acl", "SUCCESS", "role=reader")
    db.log_audit(SRC_USER, "f2:domain:partner.org", "acl", "SUCCESS", "role=commenter")
    db.log_audit(SRC_USER, "f2:new@tenantb.com", "acl", "OWED_GRANTEE_NO_ACCOUNT")
    db.log_audit(SRC_USER, "f2:(list-failed)", "acl", "SKIPPED_NO_PERMISSION")


def test_grants_are_split_inside_outside_and_public(db):
    _grants(db)
    r = S.report(db.conn, OWN)
    t = r["totals"]
    assert (t["external"]["grants"], t["external"]["files"]) == (2, 2)
    assert (t["public"]["grants"], t["internal"]["grants"], t["domain"]["grants"]) == (1, 2, 1)
    assert r["externalPeople"] == [{"name": "friend@gmail.com", "files": 2}]
    assert {d["name"] for d in r["externalDomains"]} == {"gmail.com", "partner.org"}
    assert r["notGranted"] == {"OWED_GRANTEE_NO_ACCOUNT": 1}        # owed, not made; (list-failed) is no grant
    assert r["roles"] == {"writer": 1, "reader": 3, "not recorded": 1, "commenter": 1}


def test_a_page_filters_by_kind_and_search_and_names_the_file(db):
    _grants(db)
    r = S.report(db.conn, OWN, kind="external", q="budget")
    assert r["matched"] == 1
    g, = r["grants"]
    assert (g["grantee"], g["role"], g["file"], g["targetUrl"]) == (
        "friend@gmail.com", "writer", "Budget.xlsx", "https://drive.google.com/open?id=t1")


def test_the_csv_holds_every_matching_grant(db):
    _grants(db)
    lines = S.to_csv(db.conn, OWN, kind="public").strip().splitlines()
    assert lines[0].startswith("kind,grantee,role") and len(lines) == 2 and "anyone" in lines[1]
