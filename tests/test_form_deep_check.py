"""Items are lined up by content: one missing question reads as one missing question."""
import form_deep_check as F


def q(title, kind="choiceQuestion", **extra):
    return {"itemId": "x" + title, "title": title, "questionItem": {"question": {"questionId": "q", kind: extra or {"type": "RADIO"}}}}


def test_one_missing_question_is_one_missing_question_not_every_later_one_differing():
    src = [q("Name", "textQuestion"), q("Ward"), q("Age", "textQuestion"), q("Phone", "textQuestion")]
    tgt = [q("Name", "textQuestion"), q("Age", "textQuestion"), q("Phone", "textQuestion")]
    r = F.compare_items(src, tgt)
    assert r["missing"] == ["Ward (questionItem:choiceQuestion)"] and r["changed"] == [] and r["extra"] == []


def test_a_changed_option_is_named_field_by_field_and_ids_are_ignored():
    a = q("Ward", type="RADIO", options=[{"value": "1"}, {"value": "2"}])
    b = q("Ward", type="RADIO", options=[{"value": "1"}, {"value": "3"}])
    b["itemId"] = "different-id"
    [c] = F.compare_items([a], [b])["changed"]
    assert c["item"] == "Ward" and any("options" in d for d in c["diffs"])
    assert F.compare_items([a], [dict(a, itemId="other")])["changed"] == []
