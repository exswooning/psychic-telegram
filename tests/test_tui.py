

def test_progress_never_passes_what_was_expected():
    """Live: a run read 111% (ETA -49s) once its calendar pass began -- calendar items
    counted as done against an expectation of Drive and mail only."""
    import tui
    u = tui.UserRow(source="info@s.example", target="info@t.example") if hasattr(tui, "UserRow") else None
    assert u is not None
    u.exp_drive, u.exp_mail = 136, 425
    u.drive_done, u.mail_done, u.cal_done = 136, 472, 253
    assert u.fraction == 1.0 and u.reached == 561


class TestAFirstTimeUserIsToldWhatToDo:
    """13-14 Nov 2026, a fresh install tested as a beginner: the dashboard showed the demo domains
    tenanta.com -> tenantb.com, a bar of question marks, and 'press m to start a migration'."""

    class S:
        def __init__(self, source, target):
            self.source_domain, self.target_domain = source, target

    def test_the_demo_domains_mean_not_connected(self):
        import tui
        assert not tui.is_connected(self.S("tenanta.com", "tenantb.com"))
        assert not tui.is_connected(self.S("", ""))
        assert tui.is_connected(self.S("acme.org", "acme.com"))

    def test_a_ledger_with_users_is_connected_whatever_the_domains_say(self):
        import tui
        snap = tui.Snapshot(users=[object()])
        assert tui.is_connected(self.S("tenanta.com", "tenantb.com"), snap)

    def test_the_welcome_screen_names_the_next_step_and_the_address(self):
        import tui
        text = "\n".join(tui.welcome_lines("http://10.0.0.5/app"))
        assert "http://10.0.0.5/app" in text and "Setup Wizard" in text and "bitport ui" in text

    def test_an_unknown_total_is_an_empty_bar_not_question_marks(self):
        import tui
        assert tui.bar(None, 10) == ".........."
        assert "?" not in tui.bar(None, 30)

    def test_the_recorded_address_is_read_from_the_install_info(self, tmp_path, monkeypatch):
        import tui
        (tmp_path / "install-info.txt").write_text("URL=http://1.2.3.4/app\nLOGIN=a@b.c\n")
        monkeypatch.setattr(tui.os.path, "abspath", lambda p: str(tmp_path / "tui.py"))
        assert tui.web_app_url() == "http://1.2.3.4/app"
