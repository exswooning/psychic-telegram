"""A Doc is compared by what a reader sees: adjacent runs with the same effective style are one run.

a. -> b., 17 Nov 2026: the link rewrite's replaceAllText wrote the new id with no style of its own, so the
copy read as three runs ("...spreadsheets/d/", the id, "/edit") where the source had one. Docs keeps a style
equal to the paragraph's named style as unset, so restyling cannot change that; the comparison must."""
import native_fidelity as N

ARIAL = {"bold": False, "italic": False, "backgroundColor": {}, "foregroundColor": {"color": {"rgbColor": {}}},
         "fontSize": {"magnitude": 11, "unit": "PT"}, "weightedFontFamily": {"fontFamily": "Arial", "weight": 400}}
NAMED = {"namedStyles": {"styles": [{"namedStyleType": "NORMAL_TEXT", "textStyle": {
    "bold": False, "italic": False, "fontSize": {"magnitude": 11, "unit": "PT"},
    "weightedFontFamily": {"fontFamily": "Arial", "weight": 400}}}]}}
OLD, NEW = "1vOgo-bKb2gp3kDlKZzx9CI85txxKxwxZxAZvsztyWZQ", "16B2K3Tj_r-ejHaO4O_NDu5nHvU5lvu69rkN22xhBWcI"
URL = "The numbers are in https://docs.google.com/spreadsheets/d/"


def doc(*runs):
    els, i = [], 1
    for text, style in runs:
        els.append({"startIndex": i, "endIndex": i + len(text), "textRun": {"content": text, "textStyle": style}})
        i += len(text)
    return {"tabs": [{"documentTab": {**NAMED, "body": {"content": [
        {"paragraph": {"elements": els, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}}]}}}]}


def same(a, b, lookup=lambda i: NEW if i == OLD else None):
    return N.canonical_native(N._merge_runs(a), lookup) == N.canonical_native(N._merge_runs(b))


def test_a_rewritten_link_split_into_unstyled_runs_reads_the_same_as_its_source():
    source = doc((URL + OLD + "/edit\n", ARIAL))
    target = doc((URL, ARIAL), (NEW, {}), ("/edit\n", ARIAL))
    assert same(source, target)


def test_a_real_style_difference_is_still_a_difference():
    source = doc((URL + OLD + "/edit\n", ARIAL))
    target = doc((URL, ARIAL), (NEW, {"bold": True}), ("/edit\n", ARIAL))
    assert not same(source, target)


def test_a_text_difference_is_still_a_difference():
    source = doc((URL + OLD + "/edit\n", ARIAL))
    target = doc((URL, ARIAL), (NEW, {}), ("/view\n", ARIAL))
    assert not same(source, target)


def test_the_input_is_never_changed():
    import copy
    d = doc((URL, ARIAL), (NEW, {}), ("/edit\n", ARIAL))
    before = copy.deepcopy(d)
    N._merge_runs(d)
    assert d == before
