"""Profile photos are copied source -> target (user_profiles.sync_photos); before 9 Nov 2026 they were
listed for separate review and never carried."""
import types

import user_profiles


class Dir:
    def __init__(self, photos): self.photos_, self.updates = photos, []

    def users(self):
        d = self

        class U:
            def photos(self):
                class P:
                    def get(self, userKey):
                        def go():
                            if userKey not in d.photos_:
                                raise Exception("HttpError 404 Resource Not Found: userKey")
                            return d.photos_[userKey]
                        return types.SimpleNamespace(execute=go)

                    def update(self, userKey, body):
                        d.updates.append((userKey, body))
                        d.photos_[userKey] = body
                        return types.SimpleNamespace(execute=lambda: body)
                return P()
        return U()


def test_photos_are_copied_once_and_a_user_without_one_is_left(db, settings):
    settings.source_domain, settings.target_domain = "s.x", "t.x"
    src = Dir({"a@s.x": {"photoData": "AAA", "mimeType": "JPEG"}})
    tgt = Dir({"b@t.x": {"photoData": "OLD"}})
    auth = types.SimpleNamespace(directory=lambda side, writable=False: src if side == "source" else tgt)
    pairs = [("a@s.x", "a@t.x"), ("b@s.x", "b@t.x")]
    out = user_profiles.sync_photos(auth, db, settings, pairs, apply=True, progress=lambda *_: None)
    assert (out["copied"], out["none"]) == (1, 1) and tgt.updates == [("a@t.x", {"photoData": "AAA", "mimeType": "JPEG"})]
    again = user_profiles.sync_photos(auth, db, settings, pairs, apply=True, progress=lambda *_: None)
    assert again["same"] == 1 and len(tgt.updates) == 1                      # a re-run changes nothing
