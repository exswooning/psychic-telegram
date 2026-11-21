"""Plan item 3: nothing automatic deletes on a tenant while another job writes Drive for the account.

9 Nov 2026: a test arm's end-of-run repair swept every user's staging drive while the account's full
run was copying into them -- 35 lost their source member, 5 were deleted. repair.run_all and its
staging sweep now stand down while another Drive-writing job is live. This test lists every module
that deletes on a tenant, so a new one has to be put in a group on purpose."""
import inspect
import pathlib
import re

import repair

ROOT = pathlib.Path(__file__).resolve().parent.parent
DELETES = re.compile(r"drives\(\)\.delete\(|\.batchDelete\(|messages\(\)\.delete\(|files\(\)\.delete\(|permissions\(\)\.delete\(")

GUARDED = {"repair.py": "run_all and clean_staging_drives stand down while another Drive-writing job runs"}
WRITERS = {
    "drive_engine.py": "the migration itself: deletes only its own staging drive and its own rejected attempts",
    "mirror.py": "a registered Drive-writing job; admission keeps one job per account",
}
OPERATOR = {
    "wipe_tenant.py": "Wipe data button: sandbox only, typed domain",
    "wipe_dms_mail.py": "operator action, 1-5 named users, sandbox only",
    "undo_migration.py": "operator CLI",
    "move_back.py": "operator action, confirm MOVE BACK",
    "shared_drives.py": "--cleanup-staging is an operator CLI",
    "dms_link_rewrite.py": "operator CLI, dry run unless --apply; deletes only the DMS copy it just replaced",
    "copy_safety.py": "deletes only its own test drive",
    "move_probe.py": "deletes only its own one-byte probe",
    "contract_probe.py": "probes on a scratch tenant",
    "link_transfer.py": "benchmark-only link_flip; restores what it changed",
    "form_relink.py": "operator relinking of a form it created",
}


def test_every_module_that_deletes_on_a_tenant_is_in_a_group():
    found = {p.name for p in ROOT.glob("*.py") if DELETES.search(p.read_text(encoding="utf-8", errors="replace"))}
    listed = set(GUARDED) | set(WRITERS) | set(OPERATOR)
    assert found - listed == set(), (f"{sorted(found - listed)} delete on a tenant: guard them with "
                                     "repair.drive_writers_running, or list them here with why they need none")
    assert listed - found == set(), f"listed but no longer deleting (tidy the list): {sorted(listed - found)}"


def test_the_guarded_paths_still_check():
    for fn in (repair.run_all, repair.clean_staging_drives):
        assert "drive_writers_running(" in inspect.getsource(fn), fn.__name__


def test_run_all_stands_down_beside_another_drive_job(monkeypatch, settings):
    monkeypatch.setattr(repair, "drive_writers_running", lambda aid: "migrate (pid 4242)")
    settings.account_id = 3
    out = repair.run_all(None, None, settings, apply=True)          # no auth: any real step would crash
    assert out["skipped"].endswith("migrate (pid 4242)")
