"""t16, 27 Nov 2026: an unmapped second copy of a migrated contact read IDENTICAL; a contact of the user's own does not."""
from types import SimpleNamespace

import verify_sample as V


def _svc(people):
    page = {"connections": [{"resourceName": rn, "names": [{"displayName": n}]} for rn, n in people]}
    return SimpleNamespace(people=lambda: SimpleNamespace(connections=lambda: SimpleNamespace(
        list=lambda **kw: SimpleNamespace(execute=lambda: page))))


def test_a_second_copy_is_a_duplicate_and_a_new_name_is_not():
    v = V.Verifier.__new__(V.Verifier)
    v._x = lambda fn, **kw: fn()
    res = V.Verifier._blank()
    svc = _svc([("people/c1", "Campaign Added1"), ("people/c9", "Campaign Added1"), ("people/c7", "My Own Friend"),
                ("people/c2", "Ann")])
    dup = v._contact_duplicates(svc, {"people/c1", "people/c2"}, res)
    assert [d["target"] for d in dup] == ["people/c9"] and not res["errors"]


def test_a_failed_target_read_is_an_error_not_a_clean_answer():
    v = V.Verifier.__new__(V.Verifier)
    v._x = lambda fn, **kw: (_ for _ in ()).throw(RuntimeError("HttpError 503"))
    res = V.Verifier._blank()
    assert v._contact_duplicates(_svc([]), set(), res) == [] and res["errors"]
