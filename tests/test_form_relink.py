"""form_relink: which pairs it links, and that it leaves the target as it found it but for the link."""
import form_relink
from db import bulk_seed_identities
from tests.conftest import SRC_USER, TGT_USER


class _Forms:
    def __init__(self, links):
        self.links = links

    def forms(self):
        return self

    def get(self, formId, fields):
        class _Call:
            def execute(_):
                return {"linkedSheetId": self.links.get(formId)}
        return _Call()


def _ledger(db, auth):
    bulk_seed_identities(db, [(SRC_USER, TGT_USER)])
    tgt = auth.target_drive(TGT_USER)
    form, sheet = tgt.add_binary("Job Application"), tgt.add_binary("Job Application (Responses)")
    db.record_mapping(SRC_USER, "srcForm", form, "file")
    db.record_mapping(SRC_USER, "srcSheet", sheet, "file")
    db.log_audit(SRC_USER, "srcForm", "form_link", "RELINK_BY_HAND", "sheet: srcSheet")
    # A 'Copy of' Form Google made on the source, linked to one of OUR target copies.
    db.log_audit(SRC_USER, "artefactForm", "form_link", "RELINK_BY_HAND", f"sheet: {sheet}")
    db.log_audit(SRC_USER, "lonelyForm", "form_link", "RELINK_BY_HAND", "sheet: neverCopied")
    return tgt, form, sheet


def test_only_the_organisations_own_copied_pairs_are_linked(auth, db):
    _ledger(db, auth)
    why = {p["sourceForm"]: p.get("skip") for p in form_relink.pairs(db)}
    assert why["srcForm"] is None
    assert "Bitport's own target copy" in why["artefactForm"]
    assert "has no copy on the target" in why["lonelyForm"]


def test_a_link_leaves_no_access_and_no_moved_time_behind(monkeypatch, auth, db, settings):
    tgt, form, sheet = _ledger(db, auth)
    before = {f: tgt.store[f]["modifiedTime"] for f in (form, sheet)}
    monkeypatch.setattr("scope_guard._console_login", lambda st, side: {"DWD_EMAIL": "admin@tenantb.com", "DWD_PASSWORD": "x"})
    links = {}
    monkeypatch.setattr(auth, "api", lambda tenant, name, user: _Forms(links))
    seen = {}

    def run_script(admin, todo):
        seen.update(admin=admin, todo=todo,
                    perms={f: [p.get("emailAddress") for p in tgt.perms.get(f, [])] for f in (form, sheet)})
        links[form] = sheet
        for f in (form, sheet):
            tgt.store[f]["modifiedTime"] = "2026-10-24T05:00:00Z"
        return [form]

    out = form_relink.relink(auth, db, settings, apply=True, run_script=run_script)

    assert out["linked"] == [form] and not out["errors"], out["errors"]
    assert [(p["targetForm"], p["targetSheet"]) for p in seen["todo"]] == [(form, sheet)]
    assert all("admin@tenantb.com" in seen["perms"][f] for f in (form, sheet))       # editor while it ran
    assert all("admin@tenantb.com" not in [p.get("emailAddress") for p in tgt.perms.get(f, [])] for f in (form, sheet))
    assert {f: tgt.store[f]["modifiedTime"] for f in (form, sheet)} == before
    assert [p for p in form_relink.pairs(db) if p["sourceForm"] == "srcForm"][0]["skip"] == "already relinked"


def test_a_dry_run_changes_nothing(auth, db, settings):
    _ledger(db, auth)
    out = form_relink.relink(auth, db, settings, apply=False,
                             run_script=lambda *a: (_ for _ in ()).throw(AssertionError("ran")))
    assert out["linked"] == [] and len(out["pairs"]) == 3


def test_repair_relinks_only_with_a_kept_admin_login(monkeypatch, auth, db, settings):
    import repair
    import subprocess
    _ledger(db, auth)
    settings.account_id = 5
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("launched")))
    monkeypatch.setattr("scope_guard._console_login", lambda st, side: {})
    out = repair.relink_forms(db, settings, apply=True)
    assert out["pending"] == 1 and out["errors"] == ["1 form(s) to relink by hand: no kept admin login on the target"]
    assert repair.relink_forms(db, settings, apply=False)["errors"] == []


def test_a_relinked_sheets_extra_tab_is_named_not_lumped():
    import known_issues
    assert known_issues.classify("drive", {"diffs": [
        "native Sheet entered values, formatting, or structures differ -- includes the empty responses "
        "tab Google adds when a copied Form is relinked"]}) == "drive.form_relinked"


def test_the_by_hand_list_drops_linked_pairs_and_artefacts(auth, db):
    import api_server
    _, form, sheet = _ledger(db, auth)
    listed = lambda: sorted(r["formTargetId"] for r in api_server._relinks(db.conn))   # noqa: E731
    assert listed() == ["", form]                    # the lonely one and the real one; never the artefact
    db.log_audit(SRC_USER, "srcForm", "form_link", "RELINKED", f"RELINKED: target form {form} -> sheet {sheet}")
    assert listed() == [""]
