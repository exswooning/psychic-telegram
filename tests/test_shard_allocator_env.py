"""A pass split across processes runs each shard with capped glibc arenas: without it, 8 mail threads
per shard held 45 x 64 MB arenas (2.2 GB of 3.5 GB RSS) and four shards filled the box (9 Nov 2026)."""
import inspect

import main


def test_every_shard_is_started_with_capped_arenas_and_trimming():
    src = inspect.getsource(main._run_pass_in_processes)
    assert 'env.setdefault("MALLOC_ARENA_MAX", "2")' in src
    assert 'env.setdefault("MALLOC_TRIM_THRESHOLD_"' in src
    assert src.index("MALLOC_ARENA_MAX") < src.index("subprocess.Popen(argv, env=env)")
