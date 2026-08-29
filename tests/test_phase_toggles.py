"""The full-scope button has to obey the service toggles.

It did not: its argv was fixed, so "everything except Gmail" -- what you
need when Google's Data Import owns the mailboxes -- was unsayable.
"""
import webui


def _argv(**on):
    saved = dict(webui._RUN_STATE["services"])
    try:
        webui._RUN_STATE["services"].update(on)
        return webui._action_argv("phased_migrate")
    finally:
        webui._RUN_STATE["services"].clear()
        webui._RUN_STATE["services"].update(saved)


def test_gmail_can_be_left_out():
    argv = _argv(drive=True, gmail=False, calendar=True)
    assert "gmail" not in argv
    assert "drive" in argv and "calendar" in argv


def test_shared_drives_ride_with_drive():
    assert "shared_drives" in _argv(drive=True, gmail=False)
    assert "shared_drives" not in _argv(drive=False, gmail=True)


def test_empty_selection_does_not_silently_migrate_nothing():
    argv = _argv(drive=False, gmail=False, calendar=False,
                 chat=False, contacts=False, tasks=False)
    assert "--phase" not in argv


def test_phases_py_accepts_every_name_we_pass():
    import phases
    for k in webui.PHASE_ORDER:
        assert k in phases.PHASES, k
    assert "shared_drives" in phases.PHASES
