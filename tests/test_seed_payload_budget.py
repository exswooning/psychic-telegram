"""
tests/test_seed_payload_budget.py
=================================
resources.py budgets a seeded user at ~127 MB: API client sets and their
per-thread copies. That models the CLIENTS, and for an ordinary corpus it
is right -- its files average tens of KB.

Two opt-in features break it. --big-file-mb uploads an N MB binary per
user, and --target-gb-per-user uploads 50 MB filler chunks. Both cache
their blob once, which does not help at all: _media() wraps every upload
in io.BytesIO(data), and BytesIO copies. The payload is resident once per
user IN FLIGHT. At the 18 workers this box sizes itself to, a 50 MB chunk
is 900 MB nobody charged for.

Which is the exact failure mb_per_seed_worker's own docstring describes
for SEED_MAIL_WORKERS: an input that silently invalidates the budget it
feeds.
"""

from __future__ import annotations

import pytest

import resources


@pytest.fixture(autouse=True)
def _no_payload(monkeypatch):
    """Every test states its own payload; none inherits the shell's."""
    monkeypatch.delenv("SEED_BIG_FILE_MB", raising=False)
    monkeypatch.delenv("SEED_FILLER_MB", raising=False)


class TestTheDefaultIsUnchanged:
    """Both features are off by default, and a budget that moved anyway
    would re-size every ordinary seed for a cost it never pays."""

    def test_no_payload_is_charged_when_neither_is_on(self):
        assert resources.seed_payload_mb() == 0

    def test_the_per_user_cost_is_clients_only(self):
        expected = int((resources.SEED_CLIENT_SET_MB
                        + resources.SEED_THREAD_CLIENT_MB * (4 + 11)) * 1.3)
        assert resources.mb_per_seed_worker(4, 11) == expected


class TestAPayloadIsCharged:
    def test_the_big_file_counts(self, monkeypatch):
        monkeypatch.setenv("SEED_BIG_FILE_MB", "30")
        assert resources.seed_payload_mb() == 30

    def test_the_filler_chunk_counts(self, monkeypatch):
        monkeypatch.setenv("SEED_FILLER_MB", "50")
        assert resources.seed_payload_mb() == 50

    def test_the_larger_of_the_two_wins(self, monkeypatch):
        """max, not sum: a thread does one upload at a time, and the top-up
        pass and the corpus pass do not overlap."""
        monkeypatch.setenv("SEED_BIG_FILE_MB", "30")
        monkeypatch.setenv("SEED_FILLER_MB", "50")
        assert resources.seed_payload_mb() == 50

    def test_it_reaches_the_per_user_cost(self, monkeypatch):
        before = resources.mb_per_seed_worker(4, 11)
        monkeypatch.setenv("SEED_FILLER_MB", "50")
        assert resources.mb_per_seed_worker(4, 11) > before + 50

    def test_rubbish_in_the_environment_is_not_fatal(self, monkeypatch):
        """A bad value must not take down a seed with a ValueError from
        inside the sizing code."""
        monkeypatch.setenv("SEED_BIG_FILE_MB", "lots")
        assert resources.seed_payload_mb() == 0


class TestTheBoxIsSizedSmallerForIt:
    """The point of all of the above."""

    def test_fewer_users_run_at_once(self, monkeypatch):
        plain = resources.seed_shape(3000)["workers"]
        monkeypatch.setenv("SEED_FILLER_MB", "50")
        loaded = resources.seed_shape(3000)["workers"]
        assert loaded < plain, "the box sized itself as if uploads were free"

    def test_the_budget_actually_covers_the_payload(self, monkeypatch):
        monkeypatch.setenv("SEED_FILLER_MB", "50")
        shape = resources.seed_shape(3000)
        assert shape["workers"] * 50 <= 3000, "payload alone exceeds the box"
        assert shape["per_user_mb"] >= 50

    def test_a_bigger_payload_sizes_down_further(self, monkeypatch):
        monkeypatch.setenv("SEED_FILLER_MB", "50")
        small = resources.seed_shape(3000)["workers"]
        monkeypatch.setenv("SEED_FILLER_MB", "200")
        assert resources.seed_shape(3000)["workers"] < small

    def test_it_never_sizes_below_one_user(self, monkeypatch):
        """An absurd payload must still run, one user at a time, rather
        than solving to zero workers and dying in ThreadPoolExecutor with
        'max_workers must be greater than 0'."""
        monkeypatch.setenv("SEED_FILLER_MB", "9000")
        assert resources.seed_shape(3000)["workers"] >= 1


class TestTheSeederActuallySetsIt:
    def test_the_top_up_path_publishes_its_chunk_size(self):
        """resources reads the environment; if the seeder never writes it,
        every test above passes and the live run is still unbudgeted."""
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "data-generator", "seed_sandbox.py"),
                   encoding="utf-8").read()
        assert 'os.environ["SEED_FILLER_MB"]' in src
        assert 'os.environ["SEED_BIG_FILE_MB"]' in src

    def test_it_is_set_before_the_pool_is_sized(self):
        """Set after resources.recommend() runs, it would be read too late
        and change nothing."""
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "data-generator", "seed_sandbox.py"),
                   encoding="utf-8").read()
        assert src.index('os.environ["SEED_FILLER_MB"]') \
            < src.index("resources.recommend()")

    def test_the_published_size_matches_the_chunk_actually_uploaded(self):
        """A hardcoded 50 here and a changed _FILLER_CHUNK_BYTES there is
        the drift this whole module exists to stop."""
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "data-generator", "seed_sandbox.py"),
                   encoding="utf-8").read()
        assert "_FILLER_CHUNK_BYTES // (1024 * 1024)" in src
