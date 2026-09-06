"""Drive links rot in calendar exactly as they do in mail.

"Notes are in <drive link>" in a meeting description dies with the source
tenant just like the same link in an email. Only gmail_engine rewrote
anything; calendar copied description and location verbatim.

Unlike mail, an event can be edited after the fact -- so this needs no
trash-and-reinsert, and carries none of that risk.
"""
import calendar_engine


SRC = "1SOURCEsourceSOURCEsource"
TGT = "1TARGETtargetTARGETtarget"


class _DB:
    def __init__(self, mapped=True): self.mapped = mapped
    def target_for_source_id(self, sid): return TGT if self.mapped else None
    def get_target_id(self, u, i, t): return None      # user-scoped: finds nothing
    def resolve_identity(self, e): return e


def _mig(settings, mapped=True):
    m = object.__new__(calendar_engine.CalendarMigrator)
    m.settings = settings
    m.db = _DB(mapped)
    m.source_user = "u@src.test"
    return m


class TestDescriptionsAreRewritten:
    def test_a_link_in_the_description_is_repointed(self, settings):
        settings.rewrite_drive_links = True
        m = _mig(settings)
        body = {"description": f"agenda: https://docs.google.com/document/d/{SRC}/edit"}
        assert m._rewrite_links(body) == 1
        assert TGT in body["description"] and SRC not in body["description"]

    def test_the_location_field_too(self, settings):
        settings.rewrite_drive_links = True
        m = _mig(settings)
        body = {"location": f"https://drive.google.com/open?id={SRC}"}
        assert m._rewrite_links(body) == 1
        assert TGT in body["location"]

    def test_nothing_happens_when_rewriting_is_off(self, settings):
        settings.rewrite_drive_links = False
        m = _mig(settings)
        body = {"description": f"https://docs.google.com/document/d/{SRC}/edit"}
        assert m._rewrite_links(body) == 0
        assert SRC in body["description"], "left exactly as it was"

    def test_an_unmapped_file_is_left_alone(self, settings):
        """Better a link we know is dead than one pointed somewhere wrong."""
        settings.rewrite_drive_links = True
        m = _mig(settings, mapped=False)
        body = {"description": f"https://docs.google.com/document/d/{SRC}/edit"}
        assert m._rewrite_links(body) == 0
        assert SRC in body["description"]

    def test_an_event_with_no_text_is_not_a_crash(self, settings):
        settings.rewrite_drive_links = True
        m = _mig(settings)
        assert m._rewrite_links({"summary": "standup"}) == 0


class TestAttachmentsUseTheGlobalLookup:
    def test_a_colleagues_file_is_no_longer_dropped(self, settings):
        """get_target_id is scoped to this user, so an event attaching
        someone else's document looked unmapped and had the attachment
        dropped -- for a file that had migrated perfectly well."""
        m = _mig(settings)
        out = m._map_attachments([{"fileId": SRC, "title": "spec"}])
        assert out == [{"fileId": TGT, "title": "spec"}]

    def test_a_genuinely_unmapped_attachment_is_still_dropped(self, settings):
        m = _mig(settings, mapped=False)
        assert m._map_attachments([{"fileId": SRC}]) == []


class TestItIsWiredIntoTheCopy:
    def test_build_import_body_rewrites(self):
        import inspect
        src = inspect.getsource(calendar_engine.CalendarMigrator._build_import_body)
        assert "_rewrite_links" in src, "events would copy links verbatim again"
