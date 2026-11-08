"""The durable repair queue converges (oversight watch_repair_loop, 7 Nov 2026): an item mapped since
closes on that evidence; the migration's own manifest findings reach the handlers that fix them; and a
complete source mail listing closes an unmapped message it no longer names, sampled check or not."""
import verification_repairs as R
import verify_sample as V

U = "u@src"
DRIVE_GAP = {"id": "f1", "type": "file", "status": "UNMAPPED_SOURCE",
             "error": "Pre-copy manifest item has no destination mapping"}
MAIL_GAP = {"id": "m1", "type": "message", "status": "UNMAPPED_SOURCE",
            "error": "Live source message has no migration mapping; destination coverage is not established"}


def result(**kw):
    r = V.Verifier._blank()
    r.update(checked=1, identical=1, scopeVersion=2, coverageComplete=True, **kw)
    return r


def queue(db, service, item):
    R.enqueue_findings(db, U, {service: result(sampled={"of": 100}, notCopied=[item])})


def open_ids(db):
    return sorted(r[0] for r in db.conn.execute(
        "SELECT item_id FROM verification_repair_queue WHERE state!='RESOLVED' AND category='notCopied'"))


def test_a_queued_item_mapped_since_is_closed(db):
    queue(db, "drive", DRIVE_GAP)
    queue(db, "gmail", MAIL_GAP)
    db.record_mapping(U, "f1", "t1", "file")
    assert R.reconcile_mapped(db, U) == 1
    assert open_ids(db) == ["m1"]


def test_queued_manifest_gaps_reach_the_repair_even_when_the_fresh_check_is_identical(db, settings, monkeypatch):
    R.enable(db)
    queue(db, "drive", DRIVE_GAP)
    seen = {}
    monkeypatch.setattr(R, "scoped_repair", lambda auth, db_, st, user, work: seen.update(work) or {})
    # The backlog's own check samples, so it neither lists the manifest gap nor closes it.
    R.finished_user(None, db, settings, U, {"drive": result(sampled={"of": 100})}, None, lambda f: f, lambda m: None)
    assert [i["id"] for i in seen["drive"]["notCopied"]] == ["f1"]


def test_nothing_queued_and_identical_still_skips_the_repair(db, settings, monkeypatch):
    R.enable(db)
    monkeypatch.setattr(R, "scoped_repair", lambda *a: (_ for _ in ()).throw(AssertionError("no repair")))
    assert R.finished_user(None, db, settings, U, {"drive": result()}, None, lambda f: f, lambda m: None) is None


def test_a_complete_source_mail_listing_closes_an_unmapped_message_it_no_longer_names(db):
    queue(db, "gmail", MAIL_GAP)
    R.enqueue_findings(db, U, {"gmail": result(sampled={"of": 100}, errors=["one comparison failed"])})
    assert open_ids(db) == ["m1"]                           # no listing: no evidence
    R.enqueue_findings(db, U, {"gmail": result(sampled={"of": 100}, errors=["x"], sourceCoverage=True)})
    assert open_ids(db) == []


def test_only_automatic_not_copied_kinds_are_fed_to_the_handlers(db):
    queue(db, "drive", DRIVE_GAP)
    R.enqueue_findings(db, U, {"gmail": result(sampled={"of": 100}, duplicates=[{"messageId": "x", "target": "t"}])})
    assert list(R.queued_repairable(db, U)) == ["drive"]


class _Drive:
    """files().get(fileId=...) answers from a table: an owner list, trashed, or an HTTP status."""
    def __init__(self, table):
        self.table = table

    def files(self):
        return self

    def get(self, fileId, **kw):
        def execute():
            v = self.table[fileId]
            if isinstance(v, int):
                from types import SimpleNamespace
                e = Exception(str(v))
                e.resp = SimpleNamespace(status=v)
                raise e
            return v
        from types import SimpleNamespace
        return SimpleNamespace(execute=execute)


def test_our_own_target_copies_and_files_gone_from_the_source_are_retired(db, settings):
    from types import SimpleNamespace
    settings.target_domain = "t.example"
    for fid in ("copy", "gone", "real", "broken"):
        queue(db, "drive", {**DRIVE_GAP, "id": fid})
    drive = _Drive({"copy": {"owners": [{"emailAddress": "u@t.example"}]}, "gone": 404,
                    "real": {"owners": [{"emailAddress": "other@outside.example"}]}, "broken": 500})
    auth = SimpleNamespace(source_drive=lambda u: drive)
    assert R.reconcile_target_copies(auth, db, settings, U) == 2
    assert open_ids(db) == ["broken", "real"]              # a real shared file and an unreadable one stay open


STALE = {"item": "a.pdf", "source": "11Sp-C6zeiDq3zQX006pm5ypC70IISypIc1MFUh3kuY",
         "target": "t1", "diffs": ["modified time not preserved: 2026-10-25 -> 2026-11-02"]}
SHEET_ERROR = ("file 1Bz2p_ShampUD0vMNLhHT7Gl0LwAqX5q3n3OdrlTRPWI: content could not be compared: Native sheet "
               "allocated cells exceed the explicit per-read budget")


def _stale_then_complete_check(db, **check):
    R.enqueue_findings(db, U, {"drive": result(differences=[STALE])})            # an old finding
    R.enqueue_findings(db, U, {"drive": result(**check)})                         # a later COMPLETE check


def _open_diffs(db):
    return sorted(r[0] for r in db.conn.execute(
        "SELECT item_id FROM verification_repair_queue WHERE state!='RESOLVED' AND category='differences'"))


def test_an_error_about_one_file_does_not_keep_other_files_stale_rows_open(db):
    """suchanaadhikari@: one over-large Sheet (a documented exception) kept six stale rows open for ever."""
    _stale_then_complete_check(db, errors=[SHEET_ERROR])
    assert _open_diffs(db) == []


def test_an_error_names_only_its_own_rows(db):
    _stale_then_complete_check(db, errors=[f"file {STALE['source']}: could not read the SOURCE: 503"])
    assert _open_diffs(db) == [STALE["source"]]               # the file the error is about stays open


def test_an_error_that_names_no_file_still_blocks_every_close(db):
    _stale_then_complete_check(db, errors=["could not list the target's mailbox: HTTP 503"])
    assert _open_diffs(db) == [STALE["source"]]


def test_unverified_files_keep_only_their_own_rows_open(db):
    _stale_then_complete_check(db, unverified=[{"source": STALE["source"], "target": "t1"}])
    assert _open_diffs(db) == [STALE["source"]]
