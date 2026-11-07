"""The check's own reads of a Sheet's links are paced by the user's per-minute quota as well as
the project's -- live, finance@s.: one user's Sheets reads burst past Google's 60 a minute per
user (429 RESOURCE_EXHAUSTED), six retries were spent and the Drive verdict was left INCOMPLETE."""
import drive_engine
import verify_sample as V


def test_each_read_waits_on_the_users_bucket_and_the_projects(monkeypatch):
    seen = []

    class Bucket:
        def __init__(self, rate):
            self.rate = rate

        def acquire(self, cost=1):
            seen.append(self.rate)

    monkeypatch.setattr("resilience.RateLimiter", Bucket)
    monkeypatch.setattr(drive_engine, "_native_project_limiter", lambda k, op: Bucket("project"))
    v = V.Verifier(None, None, None, "a@source.example", "a@target.example")
    v._native_pace("sheets", "read")
    v._native_pace("sheets", "read")
    per_user = drive_engine._native_rate("sheets", "read", 0)
    assert per_user == 0.9 * 60 / 60
    assert seen == [per_user, "project", per_user, "project"]
    assert len(v._native_mine) == 1      # one bucket per (API, op) for this user, reused
