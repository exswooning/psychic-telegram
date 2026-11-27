"""fableplan2 P2.6: the source probes that prove a seeded case exists (campaign_evidence), with fakes only."""
import re

import pytest

import campaign_evidence as E
import campaign_scenarios as S

U = "c03@a.example"
SHORTCUT = "application/vnd.google-apps.shortcut"


class _R:
    def __init__(self, value=None, exc=None):
        self.value, self.exc = value, exc

    def execute(self):
        if self.exc:
            raise self.exc
        return self.value


class _Err(Exception):
    def __init__(self, status, message=""):
        self.resp = type("Resp", (), {"status": status})()
        super().__init__(message)


class _Drive:
    """files.list answers by what the query names; files.get by id; comments.list from one canned answer."""
    def __init__(self, owned=(), kids=(), owners=None, comments=None, comment_error=None, media_error=None):
        self.owned, self.kids, self._owners = list(owned), list(kids), owners or {}
        self._comments, self._cerr, self._media_error = comments or [], comment_error, media_error

    def files(self):
        return self

    def comments(self):
        return self

    def list(self, **kw):
        if "comments(" in kw.get("fields", ""):
            return _R(exc=self._cerr) if self._cerr else _R({"comments": self._comments})
        if "in parents" in kw["q"]:
            return _R({"files": self.kids})
        asked = re.findall(r"(?:name|mimeType)='([^']+)'", kw["q"])
        return _R({"files": [f for f in self.owned if all(a in f["_q"] for a in asked)]})

    def get(self, fileId, **kw):
        if kw.get("alt") == "media":
            return _R(exc=self._media_error) if self._media_error else _R(b"fixture bytes")
        return _R({"owners": self._owners[fileId]}) if fileId in self._owners else _R(exc=_Err(404))


class _Auth:
    def __init__(self, drive):
        self.drive = drive

    def source_drive(self, user):
        return self.drive


class _MailAuth:
    def __init__(self, source):
        self.source = source

    def source_gmail(self, user):
        return self.source


class _Gmail:
    def __init__(self, labels=("SENT",), sender=U):
        self.labels, self.sender = list(labels), sender

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **_kw):
        return _R({"messages": [{"id": "m1"}]})

    def get(self, **_kw):
        return _R({"id": "m1", "labelIds": self.labels,
                   "payload": {"headers": [{"name": "From", "value": self.sender}]}})


def _f(name, mime="", **more):
    return {"id": name, "name": name, "mimeType": mime, "_q": name + mime, **more}


def _has(kind, drive):
    return E.source_has(kind, U, _Auth(drive), None)


def test_a_shortcut_to_a_colleagues_file_is_the_case_and_one_to_my_own_is_not():
    s = _f("s1", SHORTCUT, shortcutDetails={"targetId": "t1"})
    assert _has("drive.item_absent", _Drive([s], owners={"t1": [{"emailAddress": U}]})) is False
    assert _has("drive.item_absent", _Drive([s], owners={"t1": [{"emailAddress": "c01@a.example"}]})) is True
    assert _has("drive.item_absent", _Drive([s])) is False        # target unreadable: no proof


def test_a_form_whose_comment_list_is_refused_is_unreadable_comments_and_one_that_answers_is_not():
    form = _f("fableplan2 ward survey", E._FORM)
    for status in (403, 404):
        assert _has("drive.comments_unreadable", _Drive([form], comment_error=_Err(status))) is True
    assert _has("drive.comments_unreadable", _Drive([form])) is False
    assert _has("drive.comments_unreadable", _Drive([], comment_error=_Err(404))) is False   # no Form made
    with pytest.raises(_Err):                      # any other refusal is not evidence either way
        _has("drive.comments_unreadable", _Drive([form], comment_error=_Err(500)))


def test_only_a_reply_that_is_an_action_with_no_words_is_the_trailing_space_case():
    doc = _f("fableplan2 comments action doc")
    words = [{"replies": [{"action": "resolve", "content": "done"}]}, {"replies": [{"content": "ok"}]}]
    action = [{"replies": [{"action": "resolve", "content": ""}]}]
    assert _has("run.comment_differs", _Drive([doc], comments=words)) is False
    assert _has("run.comment_differs", _Drive([doc], comments=action)) is True
    assert _has("run.comment_differs", _Drive([], comments=action)) is False


def test_a_file_an_outsider_owns_in_the_outside_folder_is_the_unmapped_source_case():
    folder = _f("fableplan2 outside folder", "application/vnd.google-apps.folder")
    own, outsider = {"owners": [{"emailAddress": U}]}, {"owners": [{"emailAddress": "learn@gmail.com"}]}
    assert _has("drive.unmapped_source", _Drive([folder], kids=[own])) is False
    assert _has("drive.unmapped_source", _Drive([folder], kids=[])) is False
    assert _has("drive.unmapped_source", _Drive([folder], kids=[own, outsider])) is True
    assert _has("drive.unmapped_source", _Drive([], kids=[outsider])) is False        # folder never made


def test_the_malware_probe_requires_googles_exact_abusive_file_refusal():
    file = _f("eicar-test.txt")
    assert _has("run.malware_flag", _Drive([file], media_error=_Err(403, "cannotDownloadAbusiveFile"))) is True
    assert _has("run.malware_flag", _Drive([file])) is False
    assert _has("run.malware_flag", _Drive([] , media_error=_Err(403, "cannotDownloadAbusiveFile"))) is False
    with pytest.raises(_Err):
        _has("run.malware_flag", _Drive([file], media_error=_Err(403, "different refusal")))


def test_the_old_domain_sent_probe_checks_both_the_sent_label_and_source_sender():
    assert E.source_has("gmail.labels_added", U, _MailAuth(_Gmail()), None) is True
    assert E.source_has("gmail.labels_added", U, _MailAuth(_Gmail(labels=("INBOX",))), None) is False
    assert E.source_has("gmail.labels_added", U, _MailAuth(_Gmail(sender="c01@a.example")), None) is False


def test_the_form_probe_requires_an_actual_image_item_on_the_named_form(monkeypatch):
    import sys
    import types

    class Forms:
        def forms(self):
            return self

        def get(self, **_kw):
            return _R({"items": [{"imageItem": {"image": {"contentUri": "signed"}}}]})

    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", types.SimpleNamespace(build=lambda *_a, **_kw: Forms()))
    drive = _Drive([_f("fableplan2 image survey", E._FORM)])
    drive._http = object()
    assert E.source_has("run.form_rebuild", U, _Auth(drive), None) is True
    assert E.source_has("run.form_rebuild", U, _Auth(_Drive()), None) is False


def test_every_source_probe_a_scenario_reads_has_a_probe():
    assert set(S.SOURCE_PROBES) == set(E.PROBES)


def test_the_eight_once_unverified_kinds_are_each_probed_or_covered_by_a_test():
    ids = S.by_id()
    for kid in ("drive.item_absent", "drive.comments_unreadable", "run.comment_differs"):
        sc = ids[f"err:{kid}"]
        assert sc.how == "seeded" and sc.expect["or_source"] == kid and sc.fixtures, kid
    unmapped = ids["err:drive.unmapped_source"]
    assert unmapped.how == "unseedable" and unmapped.unit_test and unmapped.inject["cause"]
    for kid in ("run.held_for_drive", "drive.form_relinked", "drive.forms_response_sheet", "run.comment_missing"):
        sc = ids[f"err:{kid}"]
        assert sc.how == "unseedable" and sc.unit_test and sc.inject["cause"], kid
