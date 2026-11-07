"""Mail without a Drive link is owed to Google's DMS (SKIPPED_NO_DRIVE_LINK in the ledger), not missing.
Live (account 3, 5 Nov 2026): 822 such messages were reported as UNMAPPED_SOURCE gaps."""
from config import DEFERRED_TO_DMS
from tests.conftest import SRC_USER


def _mail_coverage(verifier, live_ids):
    class Src:
        def users(self):
            return self

        def messages(self):
            return self

        def list(self, **kw):
            class _C:
                def execute(_self):
                    return {"messages": [{"id": i} for i in live_ids]}
            return _C()

        def drafts(self):
            class _D:
                def list(_self, **kw):
                    class _E:
                        def execute(__self):
                            return {"drafts": []}
                    return _E()
            return _D()

    res = verifier._blank()
    verifier._list_all = lambda svc, side="source": iter([{"id": i} for i in live_ids])
    verifier._source_mail_coverage(Src(), res)
    return res


def test_deferred_mail_is_owed_not_a_gap(db, settings, auth, identity):
    import verify_sample as V
    v = V.Verifier(auth, db, settings, SRC_USER, "x@t")
    db.log_audit(SRC_USER, "deferred-1", "message", DEFERRED_TO_DMS, "no drive link")
    res = _mail_coverage(v, ["deferred-1", "plain-missing"])
    assert [x["id"] for x in res["notCopied"]] == ["plain-missing"]
    assert any("owed to Google's DMS" in n for n in res["notes"])
