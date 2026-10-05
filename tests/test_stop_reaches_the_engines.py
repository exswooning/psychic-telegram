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
