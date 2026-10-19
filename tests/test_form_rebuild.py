"""A Form rebuilt through the Forms API: the source is only read (forms.get), and the
target gets the questions, sections, quiz and branching. Live, dharan.gov.np: copying a
Form across organisations made Google drop a "Copy of" into the source owner's Drive."""
import copy

import native_api


class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeForms:
    def __init__(self, form=None):
        self.form, self.calls, self.n = form, [], 0

    def forms(self):
        return self

    def get(self, formId):
        self.calls.append(("get", formId))
        return _Call(lambda: self.form)

    def create(self, body):
        self.calls.append(("create", body))
        return _Call(lambda: {"formId": "NEW"})

    def setPublishSettings(self, formId, body):
        self.calls.append(("publish", body))
        return _Call(lambda: {})

    def batchUpdate(self, formId, body):
        self.calls.append(("batchUpdate", copy.deepcopy(body)))
        reqs = body["requests"]
        if reqs and "createItem" in reqs[0]:
            items = []
            for r in reqs:
                self.n += 1
                it = dict(copy.deepcopy(r["createItem"]["item"]), itemId=f"t{self.n}")
                items.append(it)
            return _Call(lambda: {"replies": [{"createItem": {"itemId": i["itemId"]}} for i in items],
                                  "form": {"items": items}})
        return _Call(lambda: {})


SOURCE = {
    "info": {"title": "Survey", "description": "Tell us"},
    "settings": {"quizSettings": {"isQuiz": True}},
    "items": [
        {"itemId": "q1", "title": "Go where?", "questionItem": {"question": {
            "questionId": "x1", "choiceQuestion": {"type": "RADIO", "options": [
                {"value": "A", "goToSectionId": "s2"}, {"value": "B", "goToAction": "NEXT_SECTION"}]}}}},
        {"itemId": "img", "imageItem": {"image": {"contentUri": "https://tmp/img", "altText": "logo"}}},
        {"itemId": "up", "title": "Attach your CV", "questionItem": {"question": {
            "questionId": "x2", "fileUploadQuestion": {"folderId": "f"}}}},
        {"itemId": "s2", "title": "Part two", "pageBreakItem": {}},
    ],
}


def test_a_form_is_rebuilt_from_a_read_of_the_source():
    src, tgt = FakeForms(SOURCE), FakeForms()
    form_id, note = native_api.copy_form(src, tgt, "SRC", "Survey")
    assert form_id == "NEW" and src.calls == [("get", "SRC")]       # the source is only read
    creates = next(b for k, b in tgt.calls if k == "batchUpdate" and "createItem" in b["requests"][0])
    items = [r["createItem"]["item"] for r in creates["requests"]]
    assert [i.get("title") for i in items] == ["Go where?", None, "Part two"]     # upload left out
    assert all("itemId" not in i for i in items)
    assert "questionId" not in items[0]["questionItem"]["question"]
    assert items[1]["imageItem"]["image"] == {"sourceUri": "https://tmp/img", "altText": "logo"}
    assert "goTo" not in str(items[0])          # every jump held back: Google takes all or none
    jump = next(b for k, b in tgt.calls if k == "batchUpdate" and "updateItem" in b["requests"][0])
    opts = jump["requests"][0]["updateItem"]["item"]["questionItem"]["question"]["choiceQuestion"]["options"]
    assert opts[0]["goToSectionId"] == "t3"                          # the TARGET's section id
    assert opts[1]["goToAction"] == "NEXT_SECTION"
    assert "1 thing(s) to add by hand: Attach your CV" in note


def test_the_quiz_and_description_are_set_and_forms_are_rebuildable():
    tgt = FakeForms()
    native_api.copy_form(FakeForms({"info": {"title": "Q", "description": "d"},
                                    "settings": {"quizSettings": {"isQuiz": True}}}), tgt, "S", "Q")
    setup = next(b for k, b in tgt.calls if k == "batchUpdate")["requests"]
    assert {"updateFormInfo", "updateSettings"} == {next(iter(r)) for r in setup}
    assert native_api.can_rebuild(native_api.FORM)


def test_a_form_closed_to_responses_stays_closed_and_an_open_one_is_left_alone():
    closed = {"info": {"title": "Q"}, "publishSettings": {"publishState": {
        "isPublished": True, "isAcceptingResponses": False}}}
    tgt = FakeForms()
    native_api.copy_form(FakeForms(closed), tgt, "S", "Q")
    assert ("publish", {"publishSettings": {"publishState": {"isPublished": True, "isAcceptingResponses": False}},
                        "updateMask": "publishState"}) in tgt.calls
    tgt = FakeForms()
    native_api.copy_form(FakeForms(dict(closed, publishSettings={"publishState": {
        "isPublished": True, "isAcceptingResponses": True}})), tgt, "S", "Q")
    assert not any(k == "publish" for k, _ in tgt.calls)


def test_repair_revisits_the_owners_of_forms_left_for_a_person(db, settings):
    """Live, dharan.gov.np: 9 Forms recorded for a person before the rebuild existed; their
    owners had finished Drive, so a resumed run never walked them again."""
    import repair
    db.log_audit("a@src", "f1", "file", "SKIPPED_UNEXPORTABLE",
                 "file: Survey\\nno API rebuild for application/vnd.google-apps.form")
    db.log_audit("b@src", "s1", "file", "SKIPPED_UNEXPORTABLE", "file: Site\\nno export mapping for application/vnd.google-apps.site")
    settings.migrate_forms = True
    assert repair.forms_to_rebuild_users(db, settings) == ["a@src"]
    settings.migrate_forms = False
    assert repair.forms_to_rebuild_users(db, settings) == []


def test_every_forms_call_goes_through_the_callers_retry():
    seen = []
    native_api.copy_form(FakeForms(SOURCE), FakeForms(), "SRC", "Survey",
                         run=lambda req: (seen.append(1), req.execute())[1])
    assert len(seen) >= 4          # get, create, setup, items, jumps -- each one, not the whole


def test_a_strategy_that_raises_is_recorded_not_hidden_behind_a_skip(migrator, monkeypatch):
    def declines(item, parent):
        migrator.db.log_audit(migrator.source_user, item["id"], "file", "SKIPPED_UNEXPORTABLE", "no export")
    def raises(item, parent):
        raise RuntimeError("429 rateLimitExceeded")
    monkeypatch.setattr(migrator, "_strategies_for", lambda item, native: [("download_upload", declines),
                                                                           ("native_api", raises)])
    migrator._sync_with_fallback({"id": "f9", "name": "Survey", "mimeType": native_api.FORM}, "p", True)
    row = migrator.db.get_audit(migrator.source_user, "f9", "file")
    assert row["status"] == "FAILED" and "429 rateLimitExceeded" in row["error_message"]
