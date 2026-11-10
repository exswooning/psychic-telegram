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


def test_a_read_that_times_out_is_retried_once_not_reported_as_a_difference(monkeypatch, db):
    """10 Nov 2026: 4 'The read operation timed out' reached the report as Form findings."""
    import types
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("The read operation timed out")
        return {"parity": True, "title": "x"}
    monkeypatch.setattr(F, "check_form", flaky)
    monkeypatch.setattr("time.sleep", lambda s: None)
    db.record_mapping("u@s.x", "src1", "tgt1", "file")
    drive = types.SimpleNamespace(files=lambda: types.SimpleNamespace(get=lambda **k: types.SimpleNamespace(
        execute=lambda: {"mimeType": F.FORM})))
    auth = types.SimpleNamespace(source_drive=lambda u: drive)
    out = F.check_user_forms(auth, db, "u@s.x", "u@t.x")
    assert len(calls) == 2 and out[0]["parity"] is True


def test_a_duplicated_image_counts_as_one_but_a_question_never_merges():
    img = {"itemId": "a", "title": "Logo", "imageItem": {"image": {"sourceUri": "x"}}}
    dup = dict(img, itemId="b")
    assert F.compare_items([img, q("Name", "textQuestion")], [img, dup, q("Name", "textQuestion")])["missing"] == []
    two_q = [q("Name", "textQuestion"), q("Name", "textQuestion")]
    assert F.compare_items(two_q, [two_q[0]])["missing"] != []


def test_an_accepted_known_difference_is_not_a_failure():
    img = {"itemId": "a", "title": "Logo", "imageItem": {"image": {"sourceUri": "x"}}}
    assert F.compare_items([img, q("Name", "textQuestion")], [q("Name", "textQuestion")], {"Logo"})["missing"] == []
    assert F.compare_items([img, q("Name", "textQuestion")], [q("Name", "textQuestion")])["missing"] != []
