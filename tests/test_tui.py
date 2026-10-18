

def test_progress_never_passes_what_was_expected():
    """Live: a run read 111% (ETA -49s) once its calendar pass began -- calendar items
    counted as done against an expectation of Drive and mail only."""
    import tui
    u = tui.UserRow(source="info@s.example", target="info@t.example") if hasattr(tui, "UserRow") else None
    assert u is not None
    u.exp_drive, u.exp_mail = 136, 425
    u.drive_done, u.mail_done, u.cal_done = 136, 472, 253
    assert u.fraction == 1.0 and u.reached == 561
