"""A seed's bar follows its own work, not users finished: a one-user seed sat at
"0/1 users", 0%, for all 38 minutes of a 30 GB run."""
import webui

HEAD = ["Seeding 1 users in src.example at scale 'medium'"]


def beat(tail):
    return "  ... still seeding: 0/1 users done after 9m00s (1 in flight), 812 calls so far" + tail


def test_content_steps_while_they_remain():
    assert webui._seed_job_pct(HEAD + [beat(" -- content: 3 of 7 steps")]) == webui._pct(3, 7)


def test_then_the_fill_once_every_user_is_planned():
    lines = HEAD + [beat(" -- content: 7 of 7 steps -- 18.4 GB uploaded of 29.9 GB planned, 1 of 1 user(s) planned")]
    assert webui._seed_job_pct(lines) == round(100 * 18.4 / 29.9, 2)


def test_not_while_the_planned_total_is_still_growing():
    lines = HEAD + [beat(" -- content: 7 of 7 steps -- 40.0 GB uploaded of 90.0 GB planned, 3 of 10 user(s) planned")]
    assert webui._seed_job_pct(lines) is None


def test_the_newest_heartbeat_wins_and_an_old_run_falls_back_to_users():
    lines = HEAD + [beat(" -- content: 1 of 7 steps"), beat(" -- content: 6 of 7 steps")]
    assert webui._seed_job_pct(lines) == webui._pct(6, 7)
    assert webui._seed_job_pct(HEAD + [beat("")]) is None


def test_the_job_progress_uses_it_for_a_seed():
    pct, _ = webui._job_progress("seed", HEAD + [beat(" -- content: 2 of 7 steps")], 600)
    assert pct == webui._pct(2, 7)
