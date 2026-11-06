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
