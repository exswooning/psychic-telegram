"""Load the seeder without leaving its directory on the process import path.

``data-generator`` contains a second ``verify.py``. Adding that directory to
``sys.path`` permanently lets it shadow the migration's guarded verifier in
later imports, so imports of the seeder must be scoped to the load itself.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading

_SEEDER = None
_LOCK = threading.Lock()


def load_seed_sandbox():
    """Return the seeder module loaded by file path, with no lasting path change."""
    global _SEEDER
    if _SEEDER is not None:
        return _SEEDER

    with _LOCK:
        if _SEEDER is not None:
            return _SEEDER

        root = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(root, "data-generator", "seed_sandbox.py")
        gen_dir = os.path.dirname(path)
        spec = importlib.util.spec_from_file_location("_bitport_seed_sandbox", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load seeder from {path}")
        module = importlib.util.module_from_spec(spec)

        # seed_sandbox imports corpus and other files beside itself. Keep this
        # path only for module execution; its verify.py must never shadow ours.
        sys.path.insert(0, gen_dir)
        try:
            spec.loader.exec_module(module)
        finally:
            if sys.path and sys.path[0] == gen_dir:
                del sys.path[0]
            else:
                try:
                    sys.path.remove(gen_dir)
                except ValueError:
                    pass

        _SEEDER = module
        return module
