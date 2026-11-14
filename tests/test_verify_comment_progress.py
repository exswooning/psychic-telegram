"""The Drive comment-thread phase reads two lists per file, one call a second, and used to report
nothing under the previous phase's label. Live (info@, 6 Nov 2026): ~50 minutes at "1,410/1,413"
that looked stuck. It now says which phase it is in and how far."""
import main
import verify_sample as V
from tests.conftest import SRC_USER, TGT_USER


def test_the_comment_phase_reports_its_own_progress(auth, db, settings, identity):
    settings.user_workers = 1
    settings.migrate_comments = True
    sd = auth.source_drive(SRC_USER)
    for n in range(3):
        sd.add_binary(f"report-{n}.pdf", data=b"quarterly %d" % n)
    main.run_batch(auth, db, settings, {"drive"}, False, 0)
    seen = []
    v = V.Verifier(auth, db, settings, SRC_USER, TGT_USER,
                   on_progress=lambda **kw: seen.append((kw.get("operation"), kw.get("completed"), kw.get("total"))))
    import verification_coverage as VC
    VC.drive(v, v.drive())
    comment = [s for s in seen if s[0] == "Drive comment threads"]
    assert comment and comment[0][1] == 0 and comment[0][2] == 3


def test_a_file_whose_comments_run_out_of_time_is_unread_not_a_stall(auth, db, settings, identity, monkeypatch):
    """ecodev@'s JPEG sat at 287s of the 300s document budget in the comment phase (7 Nov 2026). Its two list
    calls now share one budget; running over names the file and leaves the check INCOMPLETE -- never a pass --
    while the other files are still compared."""
    import migration_comments as MC
    from resilience import RetryBudgetExceeded
    settings.user_workers = 1
    settings.migrate_comments = True
    sd = auth.source_drive(SRC_USER)
    for n in range(3):
        sd.add_binary(f"report-{n}.pdf", data=b"quarterly %d" % n)
    main.run_batch(auth, db, settings, {"drive"}, False, 0)
    v = V.Verifier(auth, db, settings, SRC_USER, TGT_USER)
    slow = v._pairs("file", sample=True)[1][0]
    real = MC.list_threads

    def list_threads(svc, fid, execute=lambda fn: fn()):
        if fid == slow:
            raise RetryBudgetExceeded("this file's time budget ran out", None, 1, 0.0)
        return real(svc, fid, execute)
    monkeypatch.setattr(MC, "list_threads", list_threads)
    import verification_coverage as VC
    res = v.drive()
    VC.drive(v, res)
    comments = next(c for c in res["checks"] if c["key"] == "drive.comments")
    assert comments["status"] == "INCOMPLETE" and slow in comments["note"], comments


def test_the_current_document_does_not_outlive_its_comparison(auth, db, settings, identity):
    """The heartbeat kept showing the last compared file with a growing timer through the stray check and the
    comment phase, and read as a 22-minute hang on info@ (7 Nov 2026) though the file finished in seconds.
    It now clears when the file is done, and the comment phase names the file it is reading."""
    settings.user_workers = 1
    settings.migrate_comments = True
    sd = auth.source_drive(SRC_USER)
    for n in range(3):
        sd.add_binary(f"report-{n}.pdf", data=b"quarterly %d" % n)
    main.run_batch(auth, db, settings, {"drive"}, False, 0)
    v = V.Verifier(auth, db, settings, SRC_USER, TGT_USER)
    published = []
    real = v._publish_stats
    v._publish_stats = lambda **kw: (published.append(dict(kw)), real(**kw))[1]
    res = v.drive()
    assert v._stats["currentDocument"] is None and v._stats["currentSince"] is None   # none left after the compare
    import verification_coverage as VC
    VC.drive(v, res)
    reading = [p for p in published if p.get("currentMime") == "comment threads"]
    assert len(reading) == 3 and all(p["currentDocument"] for p in reading)           # each file, by name
    assert v._stats["currentDocument"] is None                                        # and cleared when done


def test_comment_threads_of_many_files_are_read_side_by_side_and_compared_in_order(
        auth, db, settings, identity, monkeypatch):
    """buildingpermit@'s comment phase ran three times as long as its whole file comparison (8 Nov 2026), one
    file at a time. The reads overlap now; the differences still come back in the files' order."""
    import threading
    import time
    import migration_comments as MC
    settings.user_workers = 1
    settings.migrate_comments = True
    monkeypatch.setenv("VERIFY_DRIVE_WORKERS", "6")
    sd = auth.source_drive(SRC_USER)
    for n in range(6):
        sd.add_binary(f"report-{n}.pdf", data=b"quarterly %d" % n)
    main.run_batch(auth, db, settings, {"drive"}, False, 0)
    v = V.Verifier(auth, db, settings, SRC_USER, TGT_USER)
    threads, real = set(), MC.list_threads

    def slow(svc, fid, execute=lambda fn: fn()):
        threads.add(threading.current_thread().name)
        time.sleep(0.15)
        return real(svc, fid, execute)
    monkeypatch.setattr(MC, "list_threads", slow)
    import verification_coverage as VC
    res = v.drive()
    start = time.monotonic()
    VC.drive(v, res)
    comments = next(c for c in res["checks"] if c["key"] == "drive.comments")
    assert comments["status"] in ("MATCHED", "EMPTY")
    assert len(threads) > 1                                    # more than one thread read comments
    assert time.monotonic() - start < 12 * 0.15                # 12 reads, not 12 one after another


def test_the_stop_check_still_ends_the_comment_phase(auth, db, settings, identity, monkeypatch):
    import pytest
    import migration_comments as MC
    settings.user_workers = 1
    settings.migrate_comments = True
    sd = auth.source_drive(SRC_USER)
    for n in range(3):
        sd.add_binary(f"report-{n}.pdf", data=b"quarterly %d" % n)
    main.run_batch(auth, db, settings, {"drive"}, False, 0)
    v = V.Verifier(auth, db, settings, SRC_USER, TGT_USER)
    res = v.drive()

    def boom(svc, fid, execute=lambda fn: fn()):
        raise KeyboardInterrupt("stop")
    monkeypatch.setattr(MC, "list_threads", boom)
    import verification_coverage as VC
    with pytest.raises((KeyboardInterrupt, Exception)):
        VC.drive(v, res)
