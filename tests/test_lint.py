"""The app has no undefined names and no dead imports or variables: the pyflakes rules, run by
ruff (requirements-dev.txt; ruff.toml).

Two shipped bugs were exactly this. full_setup's project probe called subprocess without
importing it, and its broad except read the NameError as "no" every time -- so an uploaded key
was thrown away for a new Cloud project. And the AI log diagnosis built the log tail, never sent
it, and asked the model to quote log lines it had not been shown."""
import glob
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_no_undefined_or_unused_names():
    ruff = shutil.which("ruff", path=os.path.dirname(sys.executable)) or shutil.which("ruff")
    if not ruff:
        pytest.skip("ruff is not installed: pip install -r requirements-dev.txt")
    files = sorted(glob.glob(os.path.join(ROOT, "*.py"))
                   + glob.glob(os.path.join(ROOT, "data-generator", "*.py")))
    r = subprocess.run([ruff, "check", "--no-cache", *files], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-4000:]
