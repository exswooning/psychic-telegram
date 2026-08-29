"""The Performance page showed a three-day-old run as if it were current.

Seen live on 2026-08-29: the page rendered 2,985,814 API calls, 2,262
failures and 1200/s limiters from a run recorded 2026-08-26T17:16:01Z --
while the header pill showed `provision-users 226s elapsed`, a different
job running right then. Nothing said the numbers were old except a
timestamp in small grey text.

Metrics are written BY the migrating process, so a finished run leaves its
last snapshot in place indefinitely; nothing overwrites it until the next
migration records its own. That is reasonable storage behaviour and a bad
default for a page someone opens to ask "how is it going".

Same fault the tenant panel had, fixed the same way: say the age, and warn
once it is old enough to mislead.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _page():
    return open(os.path.join(ROOT, "migration-webui/src/pages/Metrics.tsx"),
                encoding="utf-8").read()


class TestItSaysHowOld:
    def test_the_age_is_computed(self):
        assert "const ageOf" in _page()

    def test_the_age_is_shown_beside_the_timestamp(self):
        src = _page()
        assert "recorded {l.recordedAt}" in src
        assert "describeAge(ageOf(l.recordedAt)!)" in src

    def test_an_unparseable_date_is_not_rendered_as_just_now(self):
        """Date.parse returns NaN for junk; treating that as 0 would claim a
        broken timestamp is fresh."""
        src = _page()
        block = src.split("const ageOf")[1][:300]
        assert "Number.isNaN" in block and "return null" in block

    def test_a_negative_age_cannot_happen(self):
        # Clock skew between the recording box and the browser.
        block = _page().split("const ageOf")[1][:300]
        assert "Math.max(0" in block


class TestItWarnsWhenOldEnoughToMislead:
    def test_there_is_a_threshold(self):
        assert "STALE_AFTER_S" in _page()

    def test_the_warning_renders_above_the_timestamp(self):
        # A caveat underneath the numbers is a caveat nobody reads.
        src = _page()
        assert src.index("metrics-stale") < src.index("recorded {l.recordedAt}")

    def test_it_explains_why_the_numbers_are_frozen(self):
        """"Stale" without a reason reads like a bug in the page rather than
        the expected behaviour of a finished run."""
        src = re.sub(r"\s+", " ", _page())
        assert "written by the migrating process" in src
        assert "nothing overwrites it" in src

    def test_a_fresh_run_shows_no_warning(self):
        src = _page()
        assert "> STALE_AFTER_S" in src, "the warning must be conditional"

    def test_the_threshold_is_minutes_not_days(self):
        block = _page().split("STALE_AFTER_S =")[1][:40]
        assert "30 * 60" in block
