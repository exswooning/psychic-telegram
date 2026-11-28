import verify_sample as vs


def test_slots_follow_memory_and_the_pin(monkeypatch):
    import resources
    monkeypatch.delenv("VERIFY_CONTENT_SLOTS", raising=False)
    r = resources.probe()
    for gb, want in ((1.0, 1), (5.0, 2), (40.0, 4)):
        r.ram_usable_gb = gb
        monkeypatch.setattr(resources, "probe", lambda r=r: r)
        assert vs._content_slots() == want
    monkeypatch.setenv("VERIFY_CONTENT_SLOTS", "3")
    assert vs._content_slots() == 3
