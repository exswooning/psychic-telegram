"""fableplan2 P2.4: the cohort file, the fixture registry and the fixtures that make awkward shapes."""
import json
import os

import pytest

import corpus
import fixtures as F

HERE = os.path.dirname(os.path.abspath(__file__))
COHORT = os.path.join(HERE, "cohorts", "fableplan2.json")


def test_the_cohort_file_is_valid_and_has_the_thirty_users_of_the_plan():
    c = F.load_cohort(COHORT)
    names = [u["localpart"] for u in c["users"]]
    assert len(names) == 30 and names[:10] == [f"c{i:02d}" for i in range(1, 11)]
    assert names[10:] == [f"t{i:02d}" for i in range(1, 21)]
    assert {u.get("content_key") for u in c["users"][10:]} == {"twin"}        # twins share one content key


def test_an_unknown_fixture_param_duplicate_or_bad_name_is_refused(tmp_path):
    def write(users):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"users": users}))
        return str(p)
    for bad in ([{"localpart": "c01", "fixtures": ["nope"]}], [{"localpart": "c01", "params": {"x": 1}}],
                [{"localpart": "c01"}, {"localpart": "c01"}], [{"localpart": "Bad Name"}]):
        with pytest.raises(ValueError):
            F.load_cohort(write(bad))


def test_every_fixture_a_scenario_names_exists():
    import campaign_scenarios as S
    wanted = {f for sc in S.all_scenarios() for f in sc.fixtures if not (len(f) == 3 and f[0] in "ct" and f[1:].isdigit())}
    assert wanted <= set(F.REGISTRY), sorted(wanted - set(F.REGISTRY))


def test_entries_carry_each_users_knobs_and_twins_share_content(monkeypatch):
    monkeypatch.setattr(corpus, "CONTENT_KEYS", {})
    entries = F.entries_from_cohort(F.load_cohort(COHORT), "a.example")
    by = {e["local"]: e for e in entries}
    assert by["c01"]["edge_cases"] is True and by["c10"]["params"]["bulk_messages"] == 5000
    assert by["c04"]["fixtures"][0] == "mail_shapes" and by["t01"]["email"] == "t01@a.example"
    assert corpus.stable_seed("t01@a.example") == corpus.stable_seed("t02@a.example")
    assert corpus.stable_seed("c01@a.example") != corpus.stable_seed("c02@a.example")


class _Gmail:
    def __init__(self):
        self.raws = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q="", maxResults=1, includeSpamTrash=False):
        hit = [{"id": "m"}] if self.raws and "twice" in q else []
        return type("R", (), {"execute": lambda s: {"messages": hit}})()

    def insert(self, userId, body, media_body=None, internalDateSource=""):
        import base64
        raw = body["raw"] and base64.urlsafe_b64decode(body["raw"]) if "raw" in body else media_body.read_all()
        self.raws.append(raw)
        return type("R", (), {"execute": lambda s: {"id": "m"}})()


class _Media:
    def __init__(self, data, mimetype):
        self.data = data

    def read_all(self):
        return self.data


def _ctx(monkeypatch, gmail, items=None):
    import seed_sandbox as seed
    monkeypatch.setattr(seed, "_media", _Media)
    return F.Ctx(seed=seed, settings=None, user="c04@a.example", peers=["p1@a.example"], external="ext@example.com",
                 drive=None, gmail=gmail, cal=None, retry=lambda fn: fn, items=items or {"x": "1" + "a" * 43, "y": "1" + "b" * 43})


def test_mail_shapes_makes_every_awkward_message(monkeypatch):
    g = _Gmail()
    out = F.mail_shapes(_ctx(monkeypatch, g))
    assert out["notes"] == [] and out["made"] == len(g.raws) >= 13
    sizes = sorted(len(r) for r in g.raws)
    assert any(4.7e6 < n < 5.24e6 for n in sizes) and any(n > 5.5e6 for n in sizes)         # either side of 5 MB
    import re
    no_id = [r for r in g.raws if not re.search(rb"(?mi)^message-id:", r.split(b"\r\n\r\n")[0])]
    assert len(no_id) == 1
    long_hdr = [r for r in g.raws if b"X-Pad" in r][0]
    assert re.search(rb"(?m)^Message-ID:", long_hdr).start() > 8 * 1024                                         # past the engine's old 8 KB window
    ids = [r.split(b"Message-ID: <")[1].split(b">")[0] for r in g.raws if b"Message-ID: <" in r.split(b"\r\n\r\n")[0]]
    assert len(ids) - len(set(ids)) == 1                                                    # exactly one id appears twice
    assert any(not r.isascii() for r in g.raws) and any(b"pkcs7-mime" in r for r in g.raws)
    assert b"drive.google.com/drive/folders/" in b"".join(g.raws) and b"docs.google.com/document/d/" in b"".join(g.raws)


def test_a_refused_shape_is_a_note_not_a_crash(monkeypatch):
    class Refuses(_Gmail):
        def insert(self, **kw):
            raise RuntimeError("400 Invalid message")
    out = F.mail_shapes(_ctx(monkeypatch, Refuses()))
    assert out["made"] == 0 and len(out["notes"]) >= 13


def test_run_all_never_loses_the_other_fixtures(monkeypatch):
    monkeypatch.setitem(F.REGISTRY, "boom", lambda ctx: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setitem(F.REGISTRY, "fine", lambda ctx: {"made": 3, "notes": []})
    out = F.run_all(["boom", "fine"], _ctx(monkeypatch, _Gmail()))
    assert out["fine"]["made"] == 3 and "RuntimeError" in out["boom"]["notes"][0]


def test_the_eicar_string_is_not_written_whole_in_the_source():
    src = open(os.path.join(HERE, "fixtures.py"), encoding="utf-8").read()
    assert "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*" in src and "X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR" not in src


def test_a_fixture_runs_once_per_user(monkeypatch):
    """A --fixtures-only pass for a new fixture must not double the old ones (18 Nov 2026, c06)."""
    g = _Gmail()
    first = F.mail_shapes(_ctx(monkeypatch, g))
    again = F.mail_shapes(_ctx(monkeypatch, g))
    assert first["made"] > 0 and again == {"made": 0, "notes": ["already made"]}


def test_a_conversation_is_inserted_into_one_thread(monkeypatch):
    class Threads(_Gmail):
        bodies = []

        def insert(self, userId, body, media_body=None, internalDateSource=""):
            Threads.bodies.append(body)
            return type("R", (), {"execute": lambda s: {"id": "m", "threadId": "T1"}})()
    out = F.mail_threads(_ctx(monkeypatch, Threads()))
    assert out["made"] == 3
    assert [b.get("threadId") for b in Threads.bodies] == [None, "T1", "T1"]
