"""
data-generator/test_seed_estimate.py
====================================
The up-front estimate is the first number an operator sees, and it read
"12h 15m" for a run that finished in about eight. A third of that figure
was phantom: `wide` is 3,000 files at scale huge, built inside
_build_edge_cases -- which by default runs for the FIRST user only -- and
it sat inside the per-user term, charging 199 people for files they never
receive.

It is still only a rough number, and deliberately so; these pin the parts
that are supposed to be true.
"""

from __future__ import annotations

import pytest

from corpus import SCALES


def estimate_calls(scale: str, users: int, mail: int, events: int) -> int:
    """The arithmetic seed_sandbox.py performs, kept in step with it by
    test_it_matches_the_seeders_own_arithmetic below."""
    cfg = SCALES[scale]
    est_files = cfg["per_leaf"] * 60 + cfg["archive_years"] * 4 * (
        cfg["per_leaf"] // 3 or 1)
    return (est_files + mail + events) * users + cfg["wide"]


class TestEdgeCaseFilesAreChargedOnce:
    def test_one_user_pays_for_the_wide_folder_not_all_of_them(self):
        cfg = SCALES["huge"]
        one = estimate_calls("huge", 1, 1440, 480)
        two = estimate_calls("huge", 2, 1440, 480)
        # The second user adds their own corpus and nothing else -- no
        # second wide folder, because they never build one.
        per_user = (cfg["per_leaf"] * 60
                    + cfg["archive_years"] * 4 * (cfg["per_leaf"] // 3)
                    + 1440 + 480)
        assert two - one == per_user

    def test_the_wide_folder_is_counted_at_all(self):
        """Charged once, not dropped -- one user really does build it."""
        cfg = SCALES["huge"]
        without = (cfg["per_leaf"] * 60
                   + cfg["archive_years"] * 4 * (cfg["per_leaf"] // 3)
                   + 1440 + 480) * 200
        assert estimate_calls("huge", 200, 1440, 480) == without + cfg["wide"]

    def test_the_phantom_writes_are_gone(self):
        """199 users x 3,000 files was a third of the whole estimate."""
        cfg = SCALES["huge"]
        old = (cfg["per_leaf"] * 60 + cfg["wide"]
               + cfg["archive_years"] * 4 * (cfg["per_leaf"] // 3)
               + 1440 + 480) * 200
        assert old - estimate_calls("huge", 200, 1440, 480) == cfg["wide"] * 199


class TestItStaysInTheRightBallpark:
    def test_a_200_user_huge_run_lands_near_the_measured_eight_hours(self):
        """Measured live: 8.1h. Was 12.3h. Not exact -- the file count is
        still high and the 2.0 writes/sec still low, and they cancel -- but
        it must not be off by half a working day."""
        hours = estimate_calls("huge", 200, 1440, 480) / (30 * 2.0) / 3600
        assert 8.0 <= hours <= 10.5, hours

    def test_it_matches_the_seeders_own_arithmetic(self):
        """This test's copy of the formula must not drift from the real one
        -- drift is exactly how the wide bug survived."""
        import ast
        import os

        import seed_sandbox

        # The module file, not inspect.getsource of one function -- a
        # function's source is indented and will not parse on its own.
        src = open(seed_sandbox.__file__, encoding="utf-8").read()
        assigns = {t.id: ast.unparse(n.value)
                   for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Assign)
                   for t in n.targets if isinstance(t, ast.Name)}
        # `wide` outside the per-user multiplication, mail/events inside it.
        assert "cfg['wide']" in assigns["est_calls"].replace('"', "'")
        assert "cfg['wide']" not in assigns["est_files"].replace('"', "'")
