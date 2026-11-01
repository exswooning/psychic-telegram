"""Two comment threads with the same words on different spots of one file are told apart by
their anchors (live, finance@s.: a PDF carrying "[FINANCE, 2023-01-13]" twice read as
"missing or ambiguous" plus an "extra" thread, though both had been copied exactly)."""
from migration_comments import differences

A1, A2 = "[null,null,[[[5,[[0.67,0.84,0.91,0.86]]]]]]", "[null,null,[[[5,[[0.67,0.84,0.74,0.86]]]]]]"


def _src(anchor, t):
    return {"content": "[FINANCE, 2023-01-13]", "anchor": anchor, "createdTime": t,
            "author": {"displayName": "Finance"}, "replies": []}


def _tgt(anchor):
    return {"content": "[Finance, 2026-10-25] [FINANCE, 2023-01-13]", "anchor": anchor, "replies": []}


def test_same_words_different_anchors_are_identical():
    source = [_src(A1, "2026-10-25T01:44:41Z"), _src(A2, "2026-10-25T01:44:38Z")]
    assert differences(source, [_tgt(A2), _tgt(A1)], lambda fid: None) == []


def test_a_missing_twin_is_still_found():
    source = [_src(A1, "2026-10-25T01:44:41Z"), _src(A2, "2026-10-25T01:44:38Z")]
    assert differences(source, [_tgt(A1)], lambda fid: None) == ["comment thread missing or ambiguous"]
