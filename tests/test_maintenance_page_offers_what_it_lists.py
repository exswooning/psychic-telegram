"""A key on the page with no action behind it draws nothing, silently.

Maintenance.tsx renders `actions[key] && <JobRunner ... />`. A key with no
matching ACTIONS entry therefore renders as an empty slot -- no button, no
error, no hint. MAINTENANCE_KEYS listed 'repair_modified_times' from the
day it was written and ACTIONS never had it, so a working repair tool
(fixes modifiedTime stamped with the migration date, which neither a
re-run nor a delta corrects) was reachable only over SSH, behind a UI that
already believed it was offering it.

The `&&` is right -- an action a caller cannot run should not render a
button. The bug was the mismatch, so this asserts the two lists agree.
"""
import os
import re

import webui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _keys():
    src = open(os.path.join(ROOT, "migration-webui/src/pages/Maintenance.tsx"),
               encoding="utf-8").read()
    block = src.split("const MAINTENANCE_KEYS = [")[1].split("]")[0]
    return re.findall(r"'([^']+)'", block)


class TestThePageAndTheServerAgree:
    def test_every_listed_key_has_an_action(self):
        missing = [k for k in _keys() if k not in webui.ACTIONS]
        assert not missing, (
            f"Maintenance renders an empty slot for: {missing} -- no button, "
            "no error, nothing to click")

    def test_the_list_is_not_empty(self):
        # A guard that passes because it checks nothing is worse than none.
        assert len(_keys()) >= 5

    def test_the_repair_tool_is_among_them(self):
        assert "repair_modified_times" in _keys()


class TestTheRepairActions:
    def test_the_dry_run_does_not_apply(self):
        argv = webui.ACTIONS["repair_modified_times_dry"]["argv"]
        assert "--dry-run" in argv

    def test_the_real_one_does(self):
        argv = webui.ACTIONS["repair_modified_times"]["argv"]
        assert "--dry-run" not in argv
        assert "repair_modified_times.py" in " ".join(argv)

    def test_both_explain_why_a_re_run_will_not_fix_it(self):
        """The non-obvious part: the file is already in id_mapping so a full
        run skips it, and the source has not changed so a delta skips it
        too. Without that, the button looks redundant."""
        blurb = webui.ACTIONS["repair_modified_times_dry"]["blurb"]
        assert "delta" in blurb and "re-run" in blurb

    def test_a_dry_run_is_offered_before_the_real_one(self):
        keys = _keys()
        assert keys.index("repair_modified_times_dry") < \
            keys.index("repair_modified_times")
