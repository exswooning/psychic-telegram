"""Stop reaches the engines' own per-item checks when main.py runs as a script.

Run as `python main.py ...`, main.py is the module __main__, and `import main`
elsewhere (resilience.shutdown_requested, repair, mirror) used to load a SECOND
copy whose SHUTDOWN nothing ever set -- so every engine's per-item Stop check
read False forever. Live, a stopped Drive pass copied 1,115 more files in 11
minutes and would have run until its user's whole Drive was done.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = r'''
import runpy, sys
sys.argv = ["main.py", "--help"]
try:
    runpy.run_path("main.py", run_name="__main__")
except SystemExit:
    pass
script = sys.modules.get("main")
assert script is not None and script.__name__ == "__main__", "main.py did not register itself as main"
script.SHUTDOWN.set()          # exactly what the SIGINT handler sets
import resilience
print("STOP SEEN" if resilience.shutdown_requested() else "STOP MISSED")
'''


def test_a_stop_set_by_the_script_is_seen_by_the_engines():
    r = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, capture_output=True,
                       text=True, timeout=180)
    assert "STOP SEEN" in r.stdout, r.stdout + r.stderr[-2000:]


def test_a_run_that_stop_ended_does_not_exit_0(monkeypatch, tmp_path):
    """Live: Stop pressed on a migration, the run exited 0, the API took that as a clean
    finish and ran its follow-ons -- repair, then a tally whose auto-fix started a new
    migration of the very user just stopped, 10 minutes later."""
    import types

    import main
    args = types.SimpleNamespace(account_id=None, db=str(tmp_path / "ledger.db"), dry_run=False,
                                 workers=None, func=lambda a, s, d, au: main.SHUTDOWN.set())
    monkeypatch.setattr(main, "build_parser", lambda: types.SimpleNamespace(parse_args=lambda argv: args))
    monkeypatch.setattr(main, "setup_logging", lambda s: None)
    monkeypatch.setattr(main, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(main, "AuthManager", lambda s: None)
    main.SHUTDOWN.clear()
    try:
        assert main.main([]) == main.STOPPED_RC == 130
        main.SHUTDOWN.clear()
        args.func = lambda a, s, d, au: None
        assert main.main([]) == 0                      # a run nobody stopped still exits 0
    finally:
        main.SHUTDOWN.clear()
