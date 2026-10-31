"""A rebuilt Form compared by content: ids the target mints anew are not differences; a changed
question, a lost item or a file-upload question copy_form cannot carry are."""
import copy

import native_fidelity as NF

FORM = {"info": {"title": "Job  applications", "description": "Apply here"},
        "settings": {"quizSettings": {"isQuiz": False}},
        "items": [
            {"itemId": "a1", "title": "Name", "questionItem": {"question": {"questionId": "q1", "required": True,
                                                                             "textQuestion": {}}}},
            {"itemId": "s2", "title": "Part two", "pageBreakItem": {}},
            {"itemId": "a3", "title": "Ready?", "questionItem": {"question": {"questionId": "q3", "choiceQuestion": {
                "type": "RADIO", "options": [{"value": "Yes", "goToSectionId": "s2"}, {"value": "No"}]}}}},
            {"itemId": "a4", "title": "Logo", "imageItem": {"image": {"contentUri": "https://signed/1", "altText": "logo"}}},
        ]}


def _rebuilt(form):
    """What the target holds after copy_form: every id new, the title's spacing normalised."""
    t = copy.deepcopy(form)
    t["info"]["title"] = "Job applications"
    ids = {"a1": "X1", "s2": "X2", "a3": "X3", "a4": "X4"}
    for item in t["items"]:
        item["itemId"] = ids[item["itemId"]]
        q = (item.get("questionItem") or {}).get("question")
        if q:
            q["questionId"] = "new-" + q["questionId"]
            for o in (q.get("choiceQuestion") or {}).get("options", []):
                if "goToSectionId" in o:
                    o["goToSectionId"] = ids[o["goToSectionId"]]
        if "imageItem" in item:
            item["imageItem"]["image"]["contentUri"] = "https://signed/2"
    return t


class _Forms:
    def __init__(self, form):
        self.form = form

    def forms(self):
        return self

    def get(self, formId):
        return type("R", (), {"execute": lambda s: self.form})()


def test_a_faithful_rebuild_is_the_same_form():
    assert NF.compare_form(_Forms(FORM), _Forms(_rebuilt(FORM)), "s", "t") == []


def test_a_changed_option_is_a_difference():
    t = _rebuilt(FORM)
    t["items"][2]["questionItem"]["question"]["choiceQuestion"]["options"][1]["value"] = "Maybe"
    assert NF.compare_form(_Forms(FORM), _Forms(t), "s", "t") == ["Form item 3 (Ready?) differs"]


def test_a_branch_to_the_wrong_section_is_a_difference():
    t = _rebuilt(FORM)
    t["items"].insert(3, {"itemId": "X9", "title": "Extra section", "pageBreakItem": {}})
    t["items"][2]["questionItem"]["question"]["choiceQuestion"]["options"][0]["goToSectionId"] = "X9"
    assert any("item 3" in d for d in NF.compare_form(_Forms(FORM), _Forms(t), "s", "t"))


def test_a_lost_item_and_an_uncarried_file_upload_are_named():
    src = copy.deepcopy(FORM)
    src["items"].append({"itemId": "a5", "title": "CV", "questionItem": {"question": {"questionId": "q5",
                                                                                     "fileUploadQuestion": {}}}})
    t = _rebuilt(FORM)
    t["items"].pop(0)
    got = NF.compare_form(_Forms(src), _Forms(t), "s", "t")
    assert "1 file-upload question(s) not carried: no API can create one" in got
    assert "Form has 3 item(s) on the target, 4 expected" in got
