"""COMPLETE means an exact copy. Within-99.9% read a user one message short as complete."""
import db


def _roll(services, parity=1.0, items=None):
    users = [{"source_email": "a@src", "target_email": "a@tgt", "status": "DONE"}]
    t = {"user": "a@src", "countParity": parity, "services": services, "driveItems": items or {}}
    return db.tally_rollup(users, [t])["users"][0]["verdict"]


def test_equal_everywhere_is_complete():
    assert _roll({"drive": {"expected": 10, "target": 10}, "mail": {"expected": 1000, "target": 1000}}) == "COMPLETE"


def test_one_message_short_is_short_not_complete():
    assert _roll({"drive": {"expected": 10, "target": 10}, "mail": {"expected": 1000, "target": 999}},
                 parity=0.999) == "SHORT"


def test_more_on_the_target_is_not_an_exact_copy():
    assert _roll({"mail": {"expected": 100, "target": 102}}) == "DIFFERS"


def test_counts_equal_but_an_item_differs_is_differs():
    assert _roll({"drive": {"expected": 5, "target": 5}}, items={"differ": 1}) == "DIFFERS"


def test_a_service_nobody_could_count_does_not_decide_it():
    assert _roll({"drive": {"expected": 5, "target": 5}, "chat": {"expected": None, "target": None}}) == "COMPLETE"
