"""Setup probes delegation as the admin being set up, not the account's previous tenant
on that side (live: a reused key passed "18/18 scopes confirmed live" by impersonating
the OLD source's admin, while the new domain refused it outright)."""
import full_setup


def test_the_side_being_set_up_points_at_the_new_tenant(monkeypatch):
    class Old:
        source_domain, source_admin = "source.old.com", "info@source.old.com"
        target_domain, target_admin = "t.old.com", "info@t.old.com"
    monkeypatch.setattr(full_setup, "Settings", lambda **k: Old())
    st = full_setup._vs_for(3, "source", "gorkhatweed.com", "info@gorkhatweed.com")
    assert (st.source_domain, st.source_admin) == ("gorkhatweed.com", "info@gorkhatweed.com")
    assert st.target_admin == "info@t.old.com"


def test_without_a_side_nothing_is_overridden(monkeypatch):
    class Old:
        source_admin = "info@source.old.com"
    monkeypatch.setattr(full_setup, "Settings", lambda **k: Old())
    assert full_setup._vs_for(3).source_admin == "info@source.old.com"
