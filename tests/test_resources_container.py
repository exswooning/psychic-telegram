"""
tests/test_resources_container.py
=================================
Container and cgroup awareness.

resources.py was written against a macOS laptop with swap, where running out
of memory means a stall you can recover from. deploy_remote.py targets a VPS,
where there is usually no swap at all and the same mistake is an OOM kill with
no warning — so the module was over-provisioning on precisely the host it was
written to protect.

These fake the kernel interfaces rather than the probe functions, so they fail
if the parsing is wrong and not merely if the wiring is.
"""

from __future__ import annotations

import pytest

import resources


@pytest.fixture
def fake_fs(monkeypatch):
    """Serve a made-up /proc and /sys/fs/cgroup to the probe."""
    files: dict[str, str] = {}

    def fake_read(path: str):
        return files.get(path)

    monkeypatch.setattr(resources, "_read_first", fake_read)
    # Pin the cgroup path to the nested leaf plus its ancestors, which is the
    # shape /proc/self/cgroup produces inside a container or a systemd scope.
    monkeypatch.setattr(resources, "_cgroup_v2_dirs", lambda: [
        LEAF,
        "/sys/fs/cgroup/system.slice",
        "/sys/fs/cgroup",
    ])
    return files


# A nested cgroup, as a confined process actually gets. Using the ROOT paths
# here is what let the original bug through: the parsing was right and the
# path was wrong, so faking the root made every test pass while a real
# `systemd-run -p MemoryMax=512M` still read the host's 3.7 GB.
LEAF = "/sys/fs/cgroup/system.slice/run-test.scope"
V2_MEM_MAX = LEAF + "/memory.max"
V2_MEM_CUR = LEAF + "/memory.current"
V2_CPU_MAX = LEAF + "/cpu.max"
V1_MEM_MAX = resources._CGROUP_V1_MEM_MAX
V1_MEM_CUR = resources._CGROUP_V1_MEM_CUR
V1_CPU_QUOTA = resources._CGROUP_V1_CPU_QUOTA
V1_CPU_PERIOD = resources._CGROUP_V1_CPU_PERIOD


class TestCgroupMemory:
    def test_v2_limit_is_read(self, fake_fs):
        fake_fs[V2_MEM_MAX] = str(512 * 1024**2)      # 512 MB
        fake_fs[V2_MEM_CUR] = str(128 * 1024**2)
        limit, used = resources._cgroup_memory_gb()
        assert limit == pytest.approx(0.5, abs=0.01)
        assert used == pytest.approx(0.125, abs=0.01)

    def test_v1_is_the_fallback(self, fake_fs):
        fake_fs[V1_MEM_MAX] = str(2 * 1024**3)
        fake_fs[V1_MEM_CUR] = str(512 * 1024**2)
        limit, _ = resources._cgroup_memory_gb()
        assert limit == pytest.approx(2.0, abs=0.01)

    def test_unlimited_v2_reports_no_limit(self, fake_fs):
        """v2 says 'max', not a number. Parsing that as an integer would
        raise; treating it as a limit would be worse."""
        fake_fs[V2_MEM_MAX] = "max"
        assert resources._cgroup_memory_gb() is None

    def test_v1_sentinel_is_not_mistaken_for_a_petabyte(self, fake_fs):
        """Unlimited on v1 is PAGE_COUNTER_MAX, a real and enormous number.
        Believing it would size the pool for a machine that does not exist."""
        fake_fs[V1_MEM_MAX] = str(9223372036854771712)
        assert resources._cgroup_memory_gb() is None

    def test_absent_cgroup_is_not_an_error(self, fake_fs):
        assert resources._cgroup_memory_gb() is None

    def test_garbage_does_not_raise(self, fake_fs):
        """This module must never raise into startup."""
        fake_fs[V2_MEM_MAX] = "not-a-number"
        assert resources._cgroup_memory_gb() is None


class TestCgroupCpu:
    def test_v2_quota_becomes_a_core_count(self, fake_fs):
        fake_fs[V2_CPU_MAX] = "150000 100000"        # 1.5 cores
        assert resources._cgroup_cpu_count() == pytest.approx(1.5)

    def test_v2_unlimited(self, fake_fs):
        fake_fs[V2_CPU_MAX] = "max 100000"
        assert resources._cgroup_cpu_count() is None

    def test_v1_quota_and_period(self, fake_fs):
        fake_fs[V1_CPU_QUOTA] = "200000"
        fake_fs[V1_CPU_PERIOD] = "100000"
        assert resources._cgroup_cpu_count() == pytest.approx(2.0)

    def test_v1_unlimited_is_minus_one(self, fake_fs):
        fake_fs[V1_CPU_QUOTA] = "-1"
        fake_fs[V1_CPU_PERIOD] = "100000"
        assert resources._cgroup_cpu_count() is None


class TestProbeUsesTheTighterLimit:
    """The live bug: `docker run -m 512m` on a 64 GB host sized for 64 GB."""

    def _linux_probe(self, monkeypatch, fake_fs, meminfo_gb, cpus):
        r = resources.SystemResources(platform="linux")
        monkeypatch.setattr(resources, "_visible_cpus", lambda: cpus)

        import builtins

        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if path == "/proc/meminfo":
                import io
                return io.StringIO(
                    f"MemTotal: {int(meminfo_gb * 1024**2)} kB\n"
                    f"MemAvailable: {int(meminfo_gb * 0.9 * 1024**2)} kB\n"
                    "SwapTotal: 0 kB\nSwapFree: 0 kB\n")
            if path == "/proc/cpuinfo":
                import io
                return io.StringIO("")
            return real_open(path, *a, **kw)

        monkeypatch.setattr(builtins, "open", fake_open)
        resources._probe_linux(r)
        return r

    def test_a_container_limit_overrides_the_hosts_memory(self, monkeypatch, fake_fs):
        fake_fs[V2_MEM_MAX] = str(512 * 1024**2)
        fake_fs[V2_MEM_CUR] = str(64 * 1024**2)

        r = self._linux_probe(monkeypatch, fake_fs, meminfo_gb=64.0, cpus=32)

        assert r.container_limited
        assert r.ram_total_gb == pytest.approx(0.5, abs=0.01)
        # Usable is limit minus THIS cgroup's usage, not the host's free
        # memory -- reporting the host's is how 512 MB becomes 60 GB.
        assert r.ram_usable_gb == pytest.approx(0.4375, abs=0.02)
        assert any("cgroup memory limit" in n for n in r.notes)

    def test_a_host_roomier_than_the_cgroup_is_not_inflated(self, monkeypatch,
                                                            fake_fs):
        """A cgroup can only restrict. If the limit is larger than the machine,
        the machine still wins."""
        fake_fs[V2_MEM_MAX] = str(256 * 1024**3)
        fake_fs[V2_MEM_CUR] = "0"

        r = self._linux_probe(monkeypatch, fake_fs, meminfo_gb=4.0, cpus=2)

        assert not r.container_limited
        assert r.ram_total_gb == pytest.approx(4.0, abs=0.01)

    def test_a_cpu_quota_lowers_the_core_count(self, monkeypatch, fake_fs):
        fake_fs[V2_CPU_MAX] = "200000 100000"        # 2 cores of 32

        r = self._linux_probe(monkeypatch, fake_fs, meminfo_gb=64.0, cpus=32)

        assert r.cpu_logical == 2
        assert r.container_limited

    def test_the_recommendation_actually_shrinks_in_a_container(self, monkeypatch,
                                                                fake_fs):
        """The point of all of the above: fewer workers, not just a note."""
        fake_fs[V2_MEM_MAX] = str(512 * 1024**2)
        fake_fs[V2_MEM_CUR] = str(64 * 1024**2)
        r = self._linux_probe(monkeypatch, fake_fs, meminfo_gb=64.0, cpus=32)
        monkeypatch.setattr(resources, "probe", lambda: r)

        rec = resources.recommend()

        assert rec["user_workers"] <= 2, (
            f"sized {rec['user_workers']} workers inside a 512 MB container")


class TestSwaplessHosts:
    """Most VPSes and every default container have no swap, so swap_fraction is
    0 forever and the old pressure check could only ever fire on its second
    branch."""

    def test_a_swapless_host_low_on_memory_is_under_pressure(self):
        r = resources.SystemResources(ram_usable_gb=1.2, swap_total_gb=0.0)
        assert r.under_memory_pressure

    def test_a_swapless_host_with_room_is_fine(self):
        r = resources.SystemResources(ram_usable_gb=8.0, swap_total_gb=0.0)
        assert not r.under_memory_pressure

    def test_swap_fraction_does_not_divide_by_zero(self):
        assert resources.SystemResources(swap_total_gb=0.0).swap_fraction == 0.0


class TestFileDescriptorLimit:
    def test_raising_never_raises(self):
        """Runs during startup; a failure to tune must not stop a migration."""
        got = resources.raise_file_limit()
        assert got is None or (isinstance(got, tuple) and len(got) == 2)

    def test_the_soft_limit_ends_up_at_least_where_it_started(self):
        got = resources.raise_file_limit()
        if got is not None:
            before, after = got
            assert after >= before

    def test_it_ran_at_import(self):
        """The limit has to be lifted before anything opens a connection, and
        every entry point reaches this module during Settings construction."""
        assert hasattr(resources, "_FD_LIMIT_RESULT")


class TestVisibleCpus:
    def test_affinity_is_preferred_over_cpu_count(self, monkeypatch):
        """CPU pinning and cpuset cgroups make these differ, and sizing to
        cores you cannot run on is how 16 workers contend on two."""
        monkeypatch.setattr(resources.os, "sched_getaffinity",
                            lambda pid: {0, 1}, raising=False)
        monkeypatch.setattr(resources.os, "cpu_count", lambda: 64)
        assert resources._visible_cpus() == 2

    def test_it_falls_back_where_affinity_is_unavailable(self, monkeypatch):
        monkeypatch.delattr(resources.os, "sched_getaffinity", raising=False)
        monkeypatch.setattr(resources.os, "cpu_count", lambda: 4)
        assert resources._visible_cpus() == 4


class TestCgroupPathDiscovery:
    """
    The bug the fakes could not see.

    Limits live on the process's own cgroup, not on the root, and the root is
    unlimited on every normal system. Reading the root meant a process confined
    to 512 MB concluded it had the whole host — which is the exact
    over-provisioning this module exists to prevent, and it survived a green
    test run because the tests faked the paths they were meant to discover.
    """

    def _proc_self_cgroup(self, monkeypatch, content):
        import builtins
        import io

        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if path == "/proc/self/cgroup":
                return io.StringIO(content)
            return real_open(path, *a, **kw)

        monkeypatch.setattr(builtins, "open", fake_open)

    def test_the_processes_own_cgroup_comes_first(self, monkeypatch):
        self._proc_self_cgroup(
            monkeypatch, "0::/system.slice/run-rc3aa697.scope\n")

        dirs = resources._cgroup_v2_dirs()

        assert dirs[0] == "/sys/fs/cgroup/system.slice/run-rc3aa697.scope"

    def test_ancestors_are_searched_too(self, monkeypatch):
        """A limit on a parent slice constrains this process just as hard."""
        self._proc_self_cgroup(monkeypatch, "0::/system.slice/run-x.scope\n")

        dirs = resources._cgroup_v2_dirs()

        assert "/sys/fs/cgroup/system.slice" in dirs
        assert dirs[-1] == "/sys/fs/cgroup"

    def test_the_root_cgroup_yields_just_the_root(self, monkeypatch):
        self._proc_self_cgroup(monkeypatch, "0::/\n")
        assert resources._cgroup_v2_dirs() == ["/sys/fs/cgroup"]

    def test_a_v1_only_line_does_not_produce_a_bogus_path(self, monkeypatch):
        """v1 lines are numbered and named; only '0::' is the unified
        hierarchy. Treating '1:memory:/foo' as a v2 path reads nonsense."""
        self._proc_self_cgroup(
            monkeypatch, "1:memory:/docker/abc\n2:cpu:/docker/abc\n")
        assert resources._cgroup_v2_dirs() == ["/sys/fs/cgroup"]

    def test_a_missing_proc_file_degrades_to_the_root(self, monkeypatch):
        """No /proc at all — macOS, or a stripped container. Must not raise."""
        import builtins

        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if path == "/proc/self/cgroup":
                raise FileNotFoundError(path)
            return real_open(path, *a, **kw)

        monkeypatch.setattr(builtins, "open", fake_open)
        assert resources._cgroup_v2_dirs() == ["/sys/fs/cgroup"]

    def test_the_tightest_limit_in_the_chain_wins(self, monkeypatch):
        """Leaf unlimited, parent capped: the parent still binds."""
        monkeypatch.setattr(resources, "_cgroup_v2_dirs", lambda: [
            "/leaf", "/parent", "/sys/fs/cgroup"])
        values = {
            "/leaf/memory.max": "max",
            "/leaf/memory.current": str(32 * 1024**2),
            "/parent/memory.max": str(256 * 1024**2),
            "/sys/fs/cgroup/memory.max": "max",
        }
        monkeypatch.setattr(resources, "_read_first", values.get)

        limit, used = resources._cgroup_memory_gb()

        assert limit == pytest.approx(0.25, abs=0.01)
        assert used == pytest.approx(0.03125, abs=0.005)
