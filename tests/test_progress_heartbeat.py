"""Per-user progress for the migration's heartbeat (progress.py)."""
import progress as P


def test_a_tick_moves_progress_and_a_new_service_starts_over():
    P._reset()
    P.start("u@s", "drive", now=100)
    P.tick("u@s", "drive", 3, now=160)
    [s] = P.snapshot(now=200)
    assert (s["items"], s["noItemProgress"], s["elapsed"]) == (3, 40, 100)
    P.tick("u@s", "gmail", 1, now=210)
    assert P.snapshot(now=210)[0]["service"] == "gmail" and P.snapshot(now=210)[0]["items"] == 1
    P.finish("u@s")
    assert P.snapshot() == []


def test_the_line_is_the_shape_oversight_parses():
    P._reset()
    P.start("u@s", "drive", now=0)
    [line] = P.heartbeat_lines({"driveTargetRate": 6.49}, now=650)
    assert line.startswith("heartbeat u@s |") and "noItemProgress=650s" in line and "driveTargetRate=6.5/s" in line
