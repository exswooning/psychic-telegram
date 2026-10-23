"""A Doc or Slide with images used to read 'could not check'; its images are now
compared by the hash of their bytes (a signed URL differs on every read)."""
import native_fidelity as N


def _doc(url):
    return {"inlineObjects": {"i1": {"inlineObjectProperties": {"embeddedObject": {
        "imageProperties": {"contentUri": url}}}}},
        "body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "Plan"}}]}}]}}


class _Auth:
    def __init__(self, docs):
        self.docs = docs

    def api(self, side, name, user):
        doc = self.docs[side]

        class D:
            def documents(self):
                return self

            def get(self, **k):
                return type("R", (), {"execute": lambda s: doc})()
        return D()


def test_the_same_image_behind_different_signed_urls_is_identical():
    bytes_at = {"https://src/sig1": b"PNG-1", "https://tgt/sig2": b"PNG-1"}
    auth = _Auth({"source": _doc("https://src/sig1"), "target": _doc("https://tgt/sig2")})
    diffs, _ = N.compare_native(auth, "u", "t", "S", "T", "application/vnd.google-apps.document",
                                lambda x: None, fetch=bytes_at.__getitem__)
    assert diffs == []


def test_a_different_image_is_a_difference():
    bytes_at = {"https://src/sig1": b"PNG-1", "https://tgt/sig2": b"PNG-2"}
    auth = _Auth({"source": _doc("https://src/sig1"), "target": _doc("https://tgt/sig2")})
    diffs, _ = N.compare_native(auth, "u", "t", "S", "T", "application/vnd.google-apps.document",
                                lambda x: None, fetch=bytes_at.__getitem__)
    assert diffs == ["native document content, formatting or structure differs"]
