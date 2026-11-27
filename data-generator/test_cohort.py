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
    wanted |= {f for sc in S.all_scenarios() for f in sc.seed_fixtures}
    assert wanted <= set(F.REGISTRY), sorted(wanted - set(F.REGISTRY))


def test_entries_carry_each_users_knobs_and_twins_share_content(monkeypatch):
    monkeypatch.setattr(corpus, "CONTENT_KEYS", {})
    entries = F.entries_from_cohort(F.load_cohort(COHORT), "a.example")
    by = {e["local"]: e for e in entries}
    assert by["c01"]["edge_cases"] is True and by["c10"]["params"]["bulk_messages"] == 5000
    assert by["c04"]["fixtures"][0] == "mail_shapes" and "mail_no_message_id_link" not in by["c04"]["fixtures"]
    assert "mail_no_message_id_link" in by["c06"]["fixtures"]
    assert "mail_unmigrated_peer_link" in by["c08"]["fixtures"]
    assert "mail_old_domain_sent" in by["c05"]["fixtures"]
    assert "mail_unreadable_link" in by["c02"]["fixtures"] and "office_doc" in by["c07"]["fixtures"]
    assert "forms_with_image" in by["c09"]["fixtures"]
    assert by["t01"]["email"] == "t01@a.example"
    assert corpus.stable_seed("t01@a.example") == corpus.stable_seed("t02@a.example")
    assert corpus.stable_seed("c01@a.example") != corpus.stable_seed("c02@a.example")


class _Gmail:
    def __init__(self):
        self.raws, self.labels = [], []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q="", maxResults=1, includeSpamTrash=False):
        hit = [{"id": "m"}] if ((self.raws and "twice" in q)
                                  or ("No Message-ID link fixture" in q
                                      and any(b"No Message-ID link fixture" in raw for raw in self.raws))
                                  or ("Old-domain sent fixture" in q
                                      and any(b"Old-domain sent fixture" in raw for raw in self.raws))
                                  or ("Unmigrated peer link fixture" in q
                                      and any(b"Unmigrated peer link fixture" in raw for raw in self.raws))
                                  or ("Unreadable link fixture" in q
                                      and any(b"Unreadable link fixture" in raw for raw in self.raws))) else []
        return type("R", (), {"execute": lambda s: {"messages": hit}})()

    def insert(self, userId, body, media_body=None, internalDateSource=""):
        import base64
        raw = body["raw"] and base64.urlsafe_b64decode(body["raw"]) if "raw" in body else media_body.read_all()
        self.raws.append(raw)
        self.labels.append(body.get("labelIds", []))
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


def test_the_missing_message_id_fixture_is_a_split_link_and_idempotent(monkeypatch):
    g = _Gmail()
    ctx = _ctx(monkeypatch, g)
    first = F.mail_no_message_id_link(ctx)
    again = F.mail_no_message_id_link(ctx)
    raw = g.raws[0]
    assert first == {"made": 1, "notes": []}
    assert again == {"made": 0, "notes": ["already made"]}
    assert b"No Message-ID link fixture" in raw and b"drive.google.com/open?id=" in raw
    assert b"Message-ID:" not in raw.split(b"\r\n\r\n", 1)[0]


def test_the_old_domain_sent_fixture_is_sent_only_from_the_source_address(monkeypatch):
    g = _Gmail()
    out = F.mail_old_domain_sent(_ctx(monkeypatch, g))
    raw = g.raws[0]
    headers = raw.split(b"\r\n\r\n", 1)[0].lower()
    assert out == {"made": 1, "notes": []}
    assert g.labels == [["SENT"]]
    assert b"from: c04@a.example" in headers and b"subject: old-domain sent fixture" in headers


def test_the_unmigrated_peer_link_fixture_uses_a_real_shared_peer_file(monkeypatch):
    import seed_sandbox as seed
    monkeypatch.setattr(seed, "_media", _Media)
    peer = "c03@a.example"
    fid = "1" + "a" * 43
    drive = _FakeDrive(shared=[{"id": fid, "owners": [{"emailAddress": peer}]}])
    gmail = _Gmail()
    ctx = F.Ctx(seed=seed, settings=None, user="c08@a.example", peers=[peer], external="learn@gmail.com",
                drive=drive, gmail=gmail, cal=None, retry=lambda fn: fn, items=None)
    out = F.mail_unmigrated_peer_link(ctx)
    assert out == {"made": 1, "notes": []}
    assert f"open?id={fid}".encode() in gmail.raws[0]


def test_the_unreadable_link_fixture_uses_a_valid_shape_for_a_missing_drive_id(monkeypatch):
    gmail = _Gmail()
    out = F.mail_unreadable_link(_ctx(monkeypatch, gmail))
    assert out == {"made": 1, "notes": []}
    assert b"Unreadable link fixture" in gmail.raws[0]
    assert b"drive.google.com/open?id=1" + b"x" * 43 in gmail.raws[0]


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


# ---- the Drive fixtures of P2.6 (fakes only) ----------------------------------------------------------------
class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class _FakeDrive:
    """Records creates; list answers `shared` for a sharedWithMe query and one hit for a marker name in `made`."""
    def __init__(self, made=(), shared=(), refuse_permission=False):
        self.created, self.perms, self.comments_made, self.replies_made, self.media = [], [], [], [], []
        self.made, self.shared, self.refuse = list(made), list(shared), refuse_permission

    def files(self):
        return self

    def comments(self):
        return self

    def replies(self):
        return self

    def permissions(self):
        return self

    def list(self, q="", **kw):
        return _Call(lambda: {"files": self.shared if "sharedWithMe" in q
                              else [{"id": "x"}] if any(f"name='{n}'" in q for n in self.made) else []})

    def create(self, body=None, **kw):
        body = body or {}
        media = kw.get("media_body")
        if media is not None:
            self.media.append(media.read_all())

        def go():
            if "emailAddress" in body:
                if self.refuse:
                    raise RuntimeError("invalidSharingRequest")
                self.perms.append(body)
            elif "action" in body:
                self.replies_made.append(body)
            elif "mimeType" not in body and "content" in body:
                self.comments_made.append(body)
            else:
                self.created.append(body)
            return {"id": f"id{len(self.created) + len(self.comments_made)}"}
        return _Call(go)


def _dctx(drive, user="c03@a.example", external="learn@gmail.com"):
    return F.Ctx(seed=None, settings=None, user=user, peers=[], external=external, drive=drive, gmail=None, cal=None,
                 retry=lambda fn: fn, items=None)


def test_comment_actions_makes_replies_that_are_only_an_action(monkeypatch):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "googleapiclient.http", types.SimpleNamespace(MediaIoBaseUpload=lambda *a, **k: None))
    d = _FakeDrive()
    out = F.comment_actions(_dctx(d))
    assert out["notes"] == [] and len(d.comments_made) == 2
    assert [r["action"] for r in d.replies_made] == ["resolve", "resolve", "reopen"]
    assert all("content" not in r for r in d.replies_made)
    assert F.comment_actions(_dctx(_FakeDrive(made=["fableplan2 comments action doc"]))) == {"made": 0, "notes": ["already made"]}


def test_a_colleague_shortcut_points_at_a_file_a_colleague_owns_and_waits_when_none_is_shared():
    mine = {"id": "m", "owners": [{"emailAddress": "c03@a.example"}]}
    other_org = {"id": "o", "owners": [{"emailAddress": "p@elsewhere.com"}]}
    theirs = {"id": "t", "owners": [{"emailAddress": "c01@a.example"}]}
    d = _FakeDrive(shared=[mine, other_org, theirs])
    assert F.colleague_shortcut(_dctx(d))["made"] == 1
    assert d.created[0]["shortcutDetails"] == {"targetId": "t"} and d.created[0]["mimeType"].endswith("shortcut")
    none = _FakeDrive(shared=[mine, other_org])
    out = F.colleague_shortcut(_dctx(none))
    assert out["made"] == 0 and "shared with this user" in out["notes"][0] and not none.created


def test_the_office_doc_fixture_is_a_docx_with_an_office_mime(monkeypatch):
    import io
    import sys
    import types
    import zipfile

    monkeypatch.setitem(sys.modules, "googleapiclient.http", types.SimpleNamespace(
        MediaIoBaseUpload=lambda body, **_kw: type("Upload", (), {"read_all": lambda self: body.read()})()))
    drive = _FakeDrive()
    out = F.office_doc(_dctx(drive))
    assert out == {"made": 1, "notes": []}
    assert drive.created[0]["mimeType"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    with zipfile.ZipFile(io.BytesIO(drive.media[0])) as archive:
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= set(archive.namelist())


def test_the_forms_fixture_creates_an_image_item_with_a_source_uri(monkeypatch):
    import sys
    import types

    class Forms:
        def __init__(self):
            self.created, self.requests = [], []

        def forms(self):
            return self

        def create(self, body):
            self.created.append(body)
            return type("R", (), {"execute": lambda _s: {"formId": "form-1"}})()

        def batchUpdate(self, formId, body):
            self.requests.append((formId, body))
            return type("R", (), {"execute": lambda _s: {"replies": [{}]}})()

    forms = Forms()
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", types.SimpleNamespace(build=lambda *_a, **_kw: forms))
    drive = _FakeDrive()
    drive._http = object()
    out = F.forms_with_image(_dctx(drive))
    item = forms.requests[0][1]["requests"][0]["createItem"]["item"]
    assert out == {"made": 1, "notes": []}
    assert item["imageItem"]["image"]["sourceUri"].startswith("https://")


def test_the_outside_folder_is_shared_to_the_outside_address_and_says_what_a_person_must_do():
    d = _FakeDrive()
    out = F.outside_folder(_dctx(d))
    assert d.created[0]["mimeType"].endswith("folder") and d.perms[0]["emailAddress"] == "learn@gmail.com"
    assert d.perms[0]["role"] == "writer" and "learn@gmail.com" in out["notes"][0] and out["made"] == 2
    refused = F.outside_folder(_dctx(_FakeDrive(refuse_permission=True)))
    assert refused["made"] == 1 and len(refused["notes"]) == 2 and "invalidSharingRequest" in refused["notes"][0]


def test_the_cohort_gives_the_new_fixtures_to_the_users_the_scenarios_name():
    by = {u["localpart"]: u["fixtures"] for u in F.load_cohort(COHORT)["users"]}
    assert "outside_folder" in by["c02"] and {"comment_actions", "colleague_shortcut"} <= set(by["c03"])
