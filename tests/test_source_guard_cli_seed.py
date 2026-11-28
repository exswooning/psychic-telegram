"""28 Nov 2026: a seed started from the command line was not an admitted job, so the source guard stopped the
account's migrations over the seed's own writes to its declared-sandbox source."""
import api_server as A


def test_a_cli_seed_of_this_source_counts_as_a_source_writer():
    ps = "/opt/bitport/.venv/bin/python data-generator/seed_sandbox.py --confirm-domain a.example.test --users 290\n"
    assert A._cli_seed_running("a.example.test", ps)
    assert not A._cli_seed_running("other.example.test", ps)
    assert not A._cli_seed_running("a.example.test", "python main.py --account-id 3 migrate\n")
