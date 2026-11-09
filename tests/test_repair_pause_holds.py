"""An operator's pause of automatic repairs holds: every migration pass switched them back on, and the
repair loop started the moment the run stopped (account 3, 9 Nov 2026)."""
import verification_repairs as R


def test_an_automatic_switch_on_leaves_a_pause_alone(db):
    R.pause(db)
    R.enable(db)                         # what main.run_batch, verify_sample and a follow-on do
    assert R.enabled(db) is False


def test_only_the_operator_resumes(db):
    R.pause(db)
    R.enable(db, operator=True)
    assert R.enabled(db) is True


def test_an_account_nobody_has_set_is_switched_on_as_before(db):
    R.enable(db)
    assert R.enabled(db) is True
