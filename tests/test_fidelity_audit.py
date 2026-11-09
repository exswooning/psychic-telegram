"""Plan item 4: every mapped file compared, by the engine's own rules (fidelity_audit.compare)."""
import fidelity_audit as F

S, T = "s.x", "t.x"
resolve = {"bob@s.x": "bob@t.x"}.get


def _c(sp, tp, sm=None, tm=None, copied="2026-11-09T05:00:00Z"):
    sm = {"modifiedTime": "2024-01-01T00:00:00.000Z", **(sm or {})}
    tm = {"modifiedTime": "2024-01-01T00:00:00.000Z", **(tm or {})}
    return F.compare(sm, tm, sp, tp, resolve, S, T, copied)


def test_matching_grants_mapped_by_the_engines_rules_are_clean():
    sp = [{"type": "user", "role": "owner", "emailAddress": "me@s.x"},
          {"type": "user", "role": "writer", "emailAddress": "bob@s.x"},
          {"type": "user", "role": "reader", "emailAddress": "friend@gmail.com"},
          {"type": "domain", "role": "reader", "domain": "s.x", "allowFileDiscovery": True},
          {"type": "anyone", "role": "reader", "allowFileDiscovery": False},
          {"type": "user", "role": "organizer", "emailAddress": "admin@s.x"},
          {"type": "user", "role": "reader", "emailAddress": "inh@s.x", "permissionDetails": [{"inherited": True}]}]
    tp = [{"type": "user", "role": "writer", "emailAddress": "bob@t.x"},
          {"type": "user", "role": "reader", "emailAddress": "friend@gmail.com"},
          {"type": "domain", "role": "reader", "domain": "t.x", "allowFileDiscovery": True},
          {"type": "anyone", "role": "reader", "allowFileDiscovery": False}]
    assert _c(sp, tp) == []


def test_a_missing_grant_a_changed_role_and_link_discovery_are_named():
    sp = [{"type": "user", "role": "writer", "emailAddress": "bob@s.x"},
          {"type": "user", "role": "commenter", "emailAddress": "friend@gmail.com"},
          {"type": "anyone", "role": "reader", "allowFileDiscovery": True}]
    tp = [{"type": "user", "role": "reader", "emailAddress": "friend@gmail.com"},
          {"type": "anyone", "role": "reader", "allowFileDiscovery": False}]
    kinds = {k for k, _ in _c(sp, tp)}
    assert kinds == {"grant.missing", "grant.role", "grant.discovery"}


def test_modified_time_and_settings_but_not_a_source_edited_after_the_copy():
    assert _c([], [], tm={"modifiedTime": "2026-11-09T06:00:00.000Z"}) == [("mtime", "2024-01-01T00:00:00 -> 2026-11-09T06:00:00")]
    edited_later = _c([], [], sm={"modifiedTime": "2026-11-09T07:00:00.000Z"}, tm={"modifiedTime": "2026-11-09T06:00:00.000Z"})
    assert edited_later == []
    assert _c([], [], sm={"writersCanShare": False}, tm={"writersCanShare": True}) == [("setting.writersCanShare", "False -> True")]


def test_each_thread_builds_its_own_clients():
    """16 threads sharing one httplib2-backed client aborted the process (9 Nov 2026)."""
    import inspect
    src = inspect.getsource(F.audit_user)
    assert "threading.local()" in src and "auth.source_drive(source)" in src.split("def clients")[1]
