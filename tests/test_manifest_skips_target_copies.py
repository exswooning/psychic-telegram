"""The source manifest never counts the migration's own TARGET copies as source files owed a copy. A server-side
copy stays readable by its source caller, so those copies appear in the source user's Drive; the walk never takes
them back (drive_engine._walk_shared_with_me), but the manifest listed them, and every one stayed "no destination
mapping" for ever -- 14 for environment@, 243 for it.department@ (account 3, 8 Nov 2026)."""
from types import SimpleNamespace

import migration_acceptance as A

SRC, TGT, OUTSIDE = "s.example", "t.example", "outside.example"


def _file(fid, owner, mine=False):
    return {"id": fid, "name": fid, "mimeType": "application/pdf", "ownedByMe": mine,
            "owners": [{"emailAddress": owner}]}


def _inventory(settings, files):
    class Files:
        def list(self, **kw):
            return SimpleNamespace(execute=lambda: {"files": files})
    auth = SimpleNamespace(source_drive=lambda u: SimpleNamespace(files=lambda: Files()))
    settings.source_domain, settings.target_domain, settings.migrate_external_shares = SRC, TGT, True
    entries, errors = A.inventory(auth, None, settings, "u@" + SRC, "u@" + TGT, {"drive"})
    return sorted(key[2] for key in entries), errors


def test_target_owned_copies_are_not_source_files(settings):
    ids, errors = _inventory(settings, [
        _file("mine", "u@" + SRC, mine=True),
        _file("outside", "someone@" + OUTSIDE),
        _file("our-copy", "u@" + TGT),                          # our own target copy, shared back to the source user
        _file("colleague", "other@" + SRC),                     # a source colleague's: migrated by its owner
    ])
    assert not errors
    assert ids == ["mine", "outside"]


def test_a_file_co_owned_with_the_target_is_not_counted_either(settings):
    f = _file("both", "someone@" + OUTSIDE)
    f["owners"].append({"emailAddress": "u@" + TGT})
    ids, _ = _inventory(settings, [f])
    assert ids == []
