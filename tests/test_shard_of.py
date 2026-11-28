"""A user's process is fixed by its address, not its position (fp2-live F-kill, 24 Nov 2026: t02 ran in two shards)."""
import main


def test_two_processes_with_different_lists_never_share_a_user():
    users = [f"u{i}@a.example" for i in range(40)]
    n = 4
    seen = {}
    for k in range(n):
        # each process drops a different "already done" user before slicing, as the live shards did
        mine = [u for u in users if u != users[k]]
        for u in mine:
            if main.shard_of(u, n) == k:
                assert u not in seen, (u, seen.get(u), k)
                seen[u] = k
    assert set(users) - set(seen) <= {users[k] for k in range(n)}     # only the dropped ones are missing


def test_it_is_stable_and_case_blind():
    assert main.shard_of("T02@A.example", 8) == main.shard_of("t02@a.example", 8)
    assert all(0 <= main.shard_of(f"x{i}@a", 3) < 3 for i in range(50))
