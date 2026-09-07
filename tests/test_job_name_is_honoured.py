"""/api/job answered for whatever job was running, not the one asked about.

There is one Job per account and it is reused. Once a run finishes and the
next starts, that object carries the NEW job's name, lines and elapsed --
and the endpoint returned it regardless of the `name` asked for. A poller
watching "reset target" was handed a freshly-started "seed": same shape,
plausible numbers, wrong job.

Live, that made a completed 298,185-item deletion look like a job that had
been destroyed. The finished run was never lost -- Job.start writes a
transcript to disk and saves a result on completion, precisely so it
survives the object being reused.
"""
import webui


class _Job:
    def __init__(self, name, running):
        self._name, self._running = name, running
    def snapshot(self, since):
        return {"name": self._name, "running": self._running, "rc": None,
                "lines": ["current job output"], "elapsed": 12.0}


def _with_job(monkeypatch, name, running=True, saved=None):
    monkeypatch.setattr(webui, "get_job", lambda a: _Job(name, running))
    monkeypatch.setattr(webui, "load_job_result",
                        lambda a, n: dict(saved) if saved else None)


class TestAskingForOneJobDoesNotAnswerWithAnother:
    def test_a_finished_job_is_read_from_its_saved_result(self, monkeypatch):
        _with_job(monkeypatch, "seed", running=True,
                  saved={"name": "reset target", "rc": 0,
                         "lines": ["Removed: 240 drive root(s)"]})
        snap = webui._job_snapshot(66, 0, want="reset target")
        assert snap["name"] == "reset target"
        assert snap["rc"] == 0
        assert "Removed: 240 drive root(s)" in snap["lines"]
        assert snap["live"] is False
        assert snap["running"] is False

    def test_it_says_what_is_running_instead(self, monkeypatch):
        """Otherwise the caller cannot tell "mine finished" from "mine never
        started"."""
        _with_job(monkeypatch, "seed", running=True,
                  saved={"name": "reset target", "rc": 0, "lines": []})
        snap = webui._job_snapshot(66, 0, want="reset target")
        assert snap["now_running"] == "seed"

    def test_a_job_that_never_ran_says_so(self, monkeypatch):
        _with_job(monkeypatch, "seed", running=True, saved=None)
        snap = webui._job_snapshot(66, 0, want="reset target")
        assert snap["unknown"] is True
        assert "reset target" in snap["error"]
        assert snap["lines"] == [], "no other job's output leaks in"

    def test_asking_for_the_running_job_is_unchanged(self, monkeypatch):
        _with_job(monkeypatch, "seed", running=True)
        snap = webui._job_snapshot(66, 0, want="seed")
        assert snap["running"] is True
        assert snap["lines"] == ["current job output"]

    def test_no_name_still_returns_whatever_is_running(self, monkeypatch):
        """Every existing caller passes no name and must keep working."""
        _with_job(monkeypatch, "seed", running=True)
        snap = webui._job_snapshot(66, 0)
        assert snap["name"] == "seed" and snap["running"] is True


class TestTheEndpointPassesItThrough:
    def test_the_handler_reads_the_query_parameter(self):
        import inspect
        src = inspect.getsource(webui.Handler.do_GET)
        i = src.index('path == "/api/job"')
        assert 'query.get("name"' in src[i:i + 1400], (
            "the name is still decorative")


class TestAFreshlyRestartedProcess:
    """The case the first version of this fix got wrong, and these tests
    missed: JOBS starts empty after a restart, so the in-memory Job has no
    name at all. The comparison short-circuited on the empty string, and the
    ps scan supplied the real name a few lines later -- so the caller still
    got another job's output under the name it asked for.
    """

    def _empty_job_then_scan(self, monkeypatch, scanned, saved=None):
        monkeypatch.setattr(webui, "get_job", lambda a: _Job("", False))
        monkeypatch.setattr(webui, "_external_job_snapshot",
                            lambda since: dict(scanned) if scanned else None)
        monkeypatch.setattr(webui, "load_job_result",
                            lambda a, n: dict(saved) if saved else None)

    def test_a_scanned_job_does_not_answer_for_another_name(self, monkeypatch):
        self._empty_job_then_scan(
            monkeypatch,
            scanned={"name": "seed", "running": True, "rc": None,
                     "lines": ["seed output"]},
            saved={"name": "reset target", "rc": 0,
                   "lines": ["Removed: 240 drive root(s)"]})
        snap = webui._job_snapshot(66, 0, want="reset target")
        assert snap["name"] == "reset target"
        assert snap["lines"] == ["Removed: 240 drive root(s)"]
        assert snap["now_running"] == "seed"

    def test_an_empty_job_with_nothing_scanned_still_answers_honestly(
            self, monkeypatch):
        self._empty_job_then_scan(monkeypatch, scanned=None, saved=None)
        snap = webui._job_snapshot(66, 0, want="reset target")
        assert snap["unknown"] is True and snap["now_running"] is None

    def test_asking_for_the_scanned_job_by_name_works(self, monkeypatch):
        self._empty_job_then_scan(
            monkeypatch,
            scanned={"name": "seed", "running": True, "rc": None,
                     "lines": ["seed output"]})
        snap = webui._job_snapshot(66, 0, want="seed")
        assert snap["running"] is True and snap["lines"] == ["seed output"]
