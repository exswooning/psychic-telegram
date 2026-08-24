"""
resources.py
============
Size the worker pools to the machine actually running the job.

Why this is not just `cpu_count()`
----------------------------------
Migrating and seeding are I/O-bound, so on paper you can run far more workers
than cores. What actually bites is **memory**: every worker buffers file
content in flight, and once the machine starts swapping, a worker stalls on
disk long enough for the *socket* to time out. The failure then arrives as
`The read operation timed out` — indistinguishable from a network fault, and
it sends you debugging the wrong system entirely.

That is not hypothetical. An 8 GB M1 Air with 5.3 GB of its 6 GB swap already
in use ran five seeding workers at `medium` scale and produced exactly that:
five identical timeouts, thirty minutes, zero users seeded. The same code with
two workers at a smaller scale worked on the first try.

So "use all available resources" is implemented as "measure what is available
and fit inside it". On a large host that means scaling up; on a constrained
one it means refusing to make things worse.

    python3 resources.py            # report and recommend
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

# Fixed per-worker overhead in MB: the discovery documents, the two API
# client objects and the TLS session each thread holds. Measured, not
# guessed -- see mb_per_worker().
WORKER_BASE_MB = 40

# How many chunk-sized buffers a worker can hold at once. A download drains
# into one while an upload streams from another, so two, with the third as
# headroom for the copy that overlaps them.
CHUNK_BUFFERS_PER_WORKER = 3


def mb_per_worker(chunk_bytes: int | None = None) -> int:
    """Peak resident memory one worker needs, in MB.

    This was the constant 320, derived when a download drained through the
    google-api-client default chunk size of 100 MB. drive_engine._download_via
    has since pinned the chunk explicitly, and said in its own comment that
    doing so "makes per-worker peak memory a known quantity, which is what
    lets resources.py derive a worker count instead of guessing one" -- but
    the guess stayed here anyway, so the whole benefit went unclaimed.

    Live check on a 9-worker run: 453 MB peak for the entire process, about
    50 MB a worker against a 320 MB budget. Sized for a buffer six times
    larger than any worker can now allocate, the box ran at a fifth of the
    concurrency its RAM supported -- while CPU sat 82% idle.

    Still deliberately generous. Under-estimating this is the swap stall the
    module exists to prevent, and that failure (30 minutes of socket
    timeouts) is far worse than running a few workers short.
    """
    if chunk_bytes is None:
        # Read from the environment, NOT via Settings().
        #
        # This runs at module level (MB_PER_WORKER below), and Settings()
        # calls config._auto(), which imports this module -- so constructing
        # Settings here re-enters `resources` before recommend() is defined
        # and raises AttributeError: partially initialized module. config
        # then swallows that and hands out fallback sizing. The same circular
        # shape this codebase has already been bitten by twice.
        #
        # The env var is the same input Settings reads for this field, so
        # nothing is lost by reading it directly, and the dependency
        # disappears entirely.
        chunk_bytes = int(os.getenv("DOWNLOAD_CHUNK_BYTES", 0) or
                          8 * 1024 * 1024)
    chunk_mb = max(1, int(chunk_bytes) // (1024 * 1024))
    return max(64, WORKER_BASE_MB + CHUNK_BUFFERS_PER_WORKER * chunk_mb)


# Kept as a module-level name because callers and tests import it directly.
MB_PER_WORKER = mb_per_worker()

# The seeder's own figure, and deliberately not MB_PER_WORKER.
#
# That constant is derived from the MIGRATOR streaming a real file through
# memory (see its comment above). The seeder does no such thing: it
# generates small synthetic files, and its workers are threads sharing one
# interpreter rather than separate processes. Measured on the live VPS
# mid-run -- one seed process, 10 threads, 157 MB RSS total -- that is
# ~17 MB per worker, so charging each one 320 MB under-sized the pool by
# roughly 19x and left the run memory-bound at 9 workers on a box with
# 2.9 GB free. 64 MB keeps a ~3.7x margin over what was actually observed.
# Raised from 64 once the seeder went concurrent *within* a user. A user
# is no longer one thread with one client: it runs 4 leaf + 4 mail workers
# (SEED_LEAF_WORKERS / SEED_MAIL_WORKERS), and each resolves its own API
# client because httplib2.Http is not thread-safe. Measured on the VPS: a
# full drive+gmail+calendar client set is 22 MB, so a user costs roughly
# 22 + 7*(4+4) = 78 MB. 128 keeps a real margin over that while still
# leaving room for ~23 concurrent users on a 3 GB box.
SEED_CLIENT_SET_MB = 22     # one drive+gmail+calendar client set, measured
SEED_THREAD_CLIENT_MB = 7   # each extra worker resolves its own, measured


def mb_per_seed_worker(mail_workers: int | None = None) -> int:
    """Peak resident memory one seeded USER needs, in MB.

    The comment above states the arithmetic -- 22 + 7*(4+4) = 78 -- and then
    the constant beside it was typed as 128 and frozen. So SEED_MAIL_WORKERS
    is an environment variable that silently invalidates the budget it is an
    input to: setting it to 8 doubles the per-user thread count while the
    sizing keeps charging for 4, and the box quietly over-commits. That is
    the same failure MB_PER_WORKER had, in a smaller form.

    Both pools are sized by this one knob (leaf and mail alike), hence 2x.
    The 1.3 margin is the headroom the frozen 128 carried over its own 78
    and is kept deliberately: under-estimating this is the swap stall the
    module exists to prevent.
    """
    if mail_workers is None:
        mail_workers = max(1, int(os.getenv("SEED_MAIL_WORKERS", "4")))
    per_user = SEED_CLIENT_SET_MB + SEED_THREAD_CLIENT_MB * 2 * mail_workers
    return max(64, int(per_user * 1.3))


# Kept as a module-level name because callers and tests import it directly.
MB_PER_SEED_WORKER = mb_per_seed_worker()

# Above this fraction of swap in use, the machine is already trading disk for
# memory and more concurrency makes it strictly worse.
SWAP_DISTRESS = 0.60

# Drain trigger for the memory-pause watchdog (main.py). A hard floor (128 MB)
# plus a fraction of TOTAL RAM -- never of *usable* RAM, which shrinks as
# memory drains and would eventually sit below anything the machine can reach.
# That self-referential threshold is what made an earlier admission-gate design
# dead code on any host above ~2.5 GB.
PRESSURE_FLOOR_MB = 128
PRESSURE_FLOOR_FRACTION = 0.05

# Ceiling on the per-USER worker pool -- the threads that each own one
# source/target user pair for the duration of that user's migration.
#
# Was 16, justified as "beyond this, Google's per-user quotas bind first".
# That premise is wrong for this pool, and the comment on SEED_HARD_CAP
# directly below has always said why: per-user limits bind when many workers
# act on behalf of the SAME account, and every worker here owns a DIFFERENT
# user, on both tenants. Those ceilings scale with the worker count instead
# of being shared by it -- the identical argument that gives the seeder 32.
# The premise does hold for the intra-user file pool, which is a separate
# knob (drive_file_workers) and keeps its own limit.
#
# What actually binds is the per-PROJECT quota, and that is no longer a
# guess: the adaptive limiter finds it and backs off when Google pushes
# back. Measured on a live from-scratch run, 16 workers sustained 1,374
# items/min at 52% CPU with both project limiters climbing freely past
# 690/s -- neither CPU nor quota was the constraint, only the number of
# users being worked at once.
#
# RAM still binds first on a small box: recommend() divides usable memory by
# mb_per_worker() before this cap is consulted, and memory pressure collapses
# the pool to MIN_WORKERS regardless.
HARD_CAP = 32

# The seeder gets a higher ceiling than HARD_CAP because the quota that
# sets HARD_CAP does not apply to it. HARD_CAP is about Google's PER-USER
# limits, which bind when many workers act on behalf of the same account.
# Every seeded user is a DIFFERENT account with its own 3 writes/sec Drive
# ceiling, so those limits scale with the worker count instead of being
# shared by it. What does bind is the per-PROJECT pool (~200 req/sec): at
# the 0.85 writes/sec/user this seeder actually sustains, 32 workers is
# ~27/sec, roughly 7x inside that pool.
SEED_HARD_CAP = 32
MIN_WORKERS = 1


@dataclass
class SystemResources:
    cpu_logical: int = 1
    cpu_physical: int = 1
    ram_total_gb: float = 0.0
    ram_usable_gb: float = 0.0       # what can be had without swapping
    swap_total_gb: float = 0.0
    swap_used_gb: float = 0.0
    platform: str = ""
    # Set when a cgroup limit is tighter than the host's own figures, which is
    # the normal case in a container and on many VPSes.
    container_limited: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def swap_fraction(self) -> float:
        return (self.swap_used_gb / self.swap_total_gb) if self.swap_total_gb else 0.0

    @property
    def under_memory_pressure(self) -> bool:
        # A swapless host -- which most VPSes and every default container are
        # -- has swap_fraction 0 forever, so that first term never fires and
        # the whole heuristic used to rest on the `< 1.0` branch. It was tuned
        # on a macOS laptop *with* swap, where running out of memory means a
        # stall; without swap it means an OOM kill with no warning at all. So
        # a swapless machine gets a higher usable-memory floor instead: it has
        # nowhere to spill to, and the failure is fatal rather than slow.
        if self.swap_total_gb == 0.0:
            return self.ram_usable_gb < 1.5
        return self.swap_fraction >= SWAP_DISTRESS or self.ram_usable_gb < 1.0


# ----------------------------------------------------------------------
# cgroup awareness
#
# /proc/meminfo reports the *host's* memory and os.cpu_count() the host's
# cores, neither of which is the limit the process actually lives under once
# it is in a container or a cgroup-limited VPS. deploy_remote.py exists
# specifically to run this on a VPS, so this is the deployment path rather
# than an edge case -- and getting it wrong inverts the failure this module
# was written to prevent: it over-provisions, and instead of a swap stall you
# get an OOM kill.
# ----------------------------------------------------------------------
_CGROUP_V2_ROOT = "/sys/fs/cgroup"
_CGROUP_V1_MEM_MAX = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
_CGROUP_V1_MEM_CUR = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
_CGROUP_V1_CPU_QUOTA = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
_CGROUP_V1_CPU_PERIOD = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"

# An unlimited cgroup reports a sentinel rather than absence: "max" on v2, and
# on v1 a number so large it is really PAGE_COUNTER_MAX. Treat anything above
# this as no limit rather than as a machine with a petabyte of RAM.
_UNLIMITED_BYTES = 1 << 53


def _read_first(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _cgroup_v2_dirs() -> list[str]:
    """
    This process's own cgroup directory, then each ancestor up to the root.

    Reading /sys/fs/cgroup/memory.max directly finds the *root* cgroup, which
    is unlimited on every normal system -- so a process actually confined to
    512 MB read "max" and concluded it had the whole host. Verified against a
    live `systemd-run -p MemoryMax=512M`: the limit sat at
    /sys/fs/cgroup/system.slice/run-<id>.scope/memory.max and the root said
    "max".

    Ancestors are included because a limit set on a parent slice constrains
    this process just as hard as one set on its own leaf, and the effective
    limit is the tightest along that chain.
    """
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as fh:
            rel = None
            for line in fh:
                parts = line.strip().split(":", 2)
                # "0::<path>" is the unified (v2) hierarchy.
                if len(parts) == 3 and parts[0] == "0":
                    rel = parts[2]
                    break
    except OSError:
        return [_CGROUP_V2_ROOT]
    if rel is None:
        return [_CGROUP_V2_ROOT]

    dirs, cur = [], rel.strip("/")
    while cur:
        dirs.append(os.path.join(_CGROUP_V2_ROOT, cur))
        cur = os.path.dirname(cur)
    dirs.append(_CGROUP_V2_ROOT)
    return dirs


def _parse_limit(raw: str | None) -> int | None:
    """A cgroup byte limit, or None when it is absent or means 'unlimited'."""
    if raw is None or raw == "max":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    # v1 reports unlimited as PAGE_COUNTER_MAX -- a real, enormous number.
    # Believing it sizes the pool for a machine that does not exist.
    if value <= 0 or value >= _UNLIMITED_BYTES:
        return None
    return value


def _cgroup_memory_gb() -> tuple[float, float] | None:
    """(limit_gb, used_gb) from cgroup v2 or v1, or None if unlimited/absent."""
    tightest: int | None = None
    used_at_leaf: int = 0
    for i, d in enumerate(_cgroup_v2_dirs()):
        limit = _parse_limit(_read_first(os.path.join(d, "memory.max")))
        if limit is not None and (tightest is None or limit < tightest):
            tightest = limit
        if i == 0:
            # Usage is this cgroup's own, not an ancestor's: the headroom that
            # matters is our limit minus what we are already using.
            cur = _read_first(os.path.join(d, "memory.current"))
            try:
                used_at_leaf = int(cur) if cur is not None else 0
            except ValueError:
                used_at_leaf = 0

    if tightest is None:
        tightest = _parse_limit(_read_first(_CGROUP_V1_MEM_MAX))
        if tightest is None:
            return None
        cur = _read_first(_CGROUP_V1_MEM_CUR)
        try:
            used_at_leaf = int(cur) if cur is not None else 0
        except ValueError:
            used_at_leaf = 0

    return tightest / 1024**3, used_at_leaf / 1024**3


def _cgroup_cpu_count() -> float | None:
    """
    Effective CPU allowance from a cgroup quota, or None if unlimited.

    Same path rule as memory: the quota is on this process's own cgroup or an
    ancestor, never on the root, and the tightest along the chain wins.
    """
    tightest: float | None = None
    for d in _cgroup_v2_dirs():
        raw = _read_first(os.path.join(d, "cpu.max"))
        if raw is None:
            continue
        parts = raw.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota, period = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if period > 0 and quota > 0:
                cores = quota / period
                if tightest is None or cores < tightest:
                    tightest = cores
    if tightest is not None:
        return tightest
    quota_raw = _read_first(_CGROUP_V1_CPU_QUOTA)
    period_raw = _read_first(_CGROUP_V1_CPU_PERIOD)
    if quota_raw is None or period_raw is None:
        return None
    try:
        quota, period = int(quota_raw), int(period_raw)
    except ValueError:
        return None
    if quota <= 0 or period <= 0:      # -1 means unlimited
        return None
    return quota / period


def _visible_cpus() -> int:
    """
    Cores this process may actually run on.

    `os.cpu_count()` reports what the machine has, not what the process is
    allowed -- CPU pinning and cpuset cgroups both make those differ, and
    sizing a thread pool to cores you cannot use is how a "16 worker" run ends
    up contending on two.
    """
    try:
        return max(1, len(os.sched_getaffinity(0)))   # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def raise_file_limit() -> tuple[int, int] | None:
    """
    Lift the soft RLIMIT_NOFILE toward the hard limit.

    Each in-flight TLS connection is a file descriptor, and the per-thread
    service cache in auth.py multiplies that by the number of worker threads.
    The soft default is 256 on macOS and commonly 1024 on Linux -- low enough
    to reach, and when it is reached it arrives as a cascade of connection
    errors hours into a run, naming nothing that points at the real cause.

    Returns (before, after), or None where the platform has no such limit.
    Never raises: this runs during startup and a failure to tune must not stop
    a migration that would otherwise work.
    """
    try:
        import resource as _rlimit
    except ImportError:      # not POSIX
        return None
    try:
        soft, hard = _rlimit.getrlimit(_rlimit.RLIMIT_NOFILE)
        if soft >= hard and hard != _rlimit.RLIM_INFINITY:
            return soft, soft
        target = hard if hard != _rlimit.RLIM_INFINITY else max(soft, 8192)
        _rlimit.setrlimit(_rlimit.RLIMIT_NOFILE, (target, hard))
        return soft, target
    except (ValueError, OSError, AttributeError):
        return None


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=10).stdout
    except Exception:  # noqa: BLE001 - probing must never raise
        return ""


def _probe_macos(r: SystemResources) -> None:
    r.cpu_physical = int(_run(["sysctl", "-n", "hw.physicalcpu"]).strip() or 1)
    r.cpu_logical = int(_run(["sysctl", "-n", "hw.logicalcpu"]).strip() or 1)
    total = int(_run(["sysctl", "-n", "hw.memsize"]).strip() or 0)
    r.ram_total_gb = total / 1024**3

    # `Pages free` alone is misleading on macOS -- it keeps very little truly
    # free and reclaims from inactive/purgeable/speculative on demand. Those
    # are available without swapping; compressed pages are not.
    page, free, inactive, purgeable, spec = 4096, 0, 0, 0, 0
    for line in _run(["vm_stat"]).splitlines():
        digits = "".join(ch for ch in line.split(":")[-1] if ch.isdigit())
        if "page size of" in line:
            page = int(digits) if digits else 4096
        elif not digits:
            continue
        elif line.startswith("Pages free"):
            free = int(digits)
        elif line.startswith("Pages inactive"):
            inactive = int(digits)
        elif line.startswith("Pages purgeable"):
            purgeable = int(digits)
        elif line.startswith("Pages speculative"):
            spec = int(digits)
    r.ram_usable_gb = (free + inactive + purgeable + spec) * page / 1024**3

    # "total = 6144.00M  used = 5324.81M  free = 819.19M"
    for part in _run(["sysctl", "-n", "vm.swapusage"]).split():
        pass
    swap = _run(["sysctl", "-n", "vm.swapusage"])
    for key, attr in (("total", "swap_total_gb"), ("used", "swap_used_gb")):
        marker = f"{key} = "
        if marker in swap:
            val = swap.split(marker, 1)[1].split()[0]
            mult = 1024 if val.endswith("G") else 1
            try:
                setattr(r, attr, float(val.rstrip("MG")) * mult / 1024)
            except ValueError:
                pass


def _probe_linux(r: SystemResources) -> None:
    r.cpu_logical = _visible_cpus()
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            cores = {ln.split(":")[1].strip() for ln in fh
                     if ln.startswith("core id")}
        r.cpu_physical = len(cores) or r.cpu_logical
    except OSError:
        r.cpu_physical = r.cpu_logical

    info = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0]) / 1024**2   # kB -> GB
    except OSError:
        info = {}
    r.ram_total_gb = info.get("MemTotal", 0.0)
    # MemAvailable is the kernel's own estimate and better than free+cached.
    r.ram_usable_gb = info.get("MemAvailable", info.get("MemFree", 0.0))
    r.swap_total_gb = info.get("SwapTotal", 0.0)
    r.swap_used_gb = r.swap_total_gb - info.get("SwapFree", 0.0)

    # A cgroup limit overrides the host figures whenever it is tighter. Only
    # tighter: a cgroup can restrict what this process may use, never grant it
    # more than the machine has, so taking the minimum is always right.
    cg_mem = _cgroup_memory_gb()
    if cg_mem is not None:
        limit_gb, used_gb = cg_mem
        if r.ram_total_gb == 0.0 or limit_gb < r.ram_total_gb:
            r.container_limited = True
            r.ram_total_gb = limit_gb
            # MemAvailable is the host's; inside a cgroup the headroom that
            # matters is limit minus this cgroup's own usage. Reporting the
            # host's free memory here is precisely how a 512 MB container
            # decides it has 60 GB to play with.
            r.ram_usable_gb = max(0.0, limit_gb - used_gb)
            r.notes.append(
                f"cgroup memory limit {limit_gb:.2f} GB (host reports "
                f"{info.get('MemTotal', 0.0):.1f} GB); sizing to the limit")

    cg_cpu = _cgroup_cpu_count()
    if cg_cpu is not None:
        effective = max(1, int(cg_cpu))
        if effective < r.cpu_logical:
            r.container_limited = True
            r.notes.append(
                f"cgroup CPU quota {cg_cpu:.2f} cores (host shows "
                f"{r.cpu_logical}); sizing to the quota")
            r.cpu_logical = effective
            r.cpu_physical = min(r.cpu_physical, effective)


# Raised once, at import, and remembered so describe() can report it.
#
# At import rather than on demand because the limit has to be lifted before
# anything opens a connection, and every entry point (main, webui, tui, the
# standalone scripts) reaches this module through config._auto() during
# Settings construction -- which happens before any engine exists. A tuple of
# (before, after), or None where the platform has no such limit.
_FD_LIMIT_RESULT: tuple[int, int] | None = raise_file_limit()


def probe() -> SystemResources:
    r = SystemResources(platform=sys.platform)
    try:
        if sys.platform == "darwin":
            _probe_macos(r)
        elif sys.platform.startswith("linux"):
            _probe_linux(r)
        else:
            r.cpu_logical = r.cpu_physical = _visible_cpus()
            r.notes.append(f"no memory probe for {sys.platform}; assuming 4 GB usable")
            r.ram_total_gb = r.ram_usable_gb = 4.0
    except Exception as exc:  # noqa: BLE001
        r.notes.append(f"probe failed ({exc}); using conservative defaults")
    if r.cpu_logical < 1:
        r.cpu_logical = _visible_cpus()
    return r


def pressure_severe(resources: SystemResources) -> bool:
    """
    True when the machine is close to catastrophic memory failure.

    Single-sample predicate; the watchdog in main.py applies the sustained-N
    rule on top. A host with swap signals distress through swap use -- that is
    where its memory goes when it runs out. A swapless host, the production
    path, has nowhere to spill and low usable RAM is fatal, so it gets a hard
    floor: max(128 MB, 5% of TOTAL RAM). The floor is computed once from total
    RAM so it cannot shrink as memory drains.
    """
    if resources.swap_total_gb > 0:
        return resources.swap_fraction >= SWAP_DISTRESS
    floor_gb = max(PRESSURE_FLOOR_MB / 1024.0,
                   PRESSURE_FLOOR_FRACTION * resources.ram_total_gb)
    return resources.ram_usable_gb < floor_gb


_probe_cache: SystemResources | None = None
_probe_cache_until = 0.0


def cached_probe(ttl: float = 3.0) -> SystemResources:
    """
    probe() with a short TTL, for the watchdog's ~2 s polling loop.

    probe() shells out to sysctl/vm_stat on macOS and re-reads /proc on Linux;
    recomputing it every loop is wasteful and a 2-5 s old snapshot is plenty
    for a drain decision. Single global, single reader (the watchdog); a stale
    read is harmless and a duplicate probe is harmless, so no lock is needed.
    """
    global _probe_cache, _probe_cache_until
    now = time.monotonic()
    if _probe_cache is None or now >= _probe_cache_until:
        _probe_cache = probe()
        _probe_cache_until = now + ttl
    return _probe_cache


def recommend(r: SystemResources | None = None,
              concurrent_jobs: int = 1) -> dict:
    """
    Worker counts this machine can actually sustain.

    CPU is not part of this calculation at all. The pools are I/O-bound
    (each worker mostly waits on the network, not the CPU), and Google's own
    Drive migration guidance recommends distributing work across many
    per-user workers (10-50) precisely because the real ceiling is per-user
    API quota, not compute. A core-count multiplier used to cap this anyway
    -- x2, then x4 after x2 still bound before RAM or HARD_CAP got a say --
    and live evidence showed x4 was *still* too conservative: an 8-worker
    run this sized itself (2 logical cores x4) on a 2-core VPS sustained
    under 7% CPU for a full 9-hour, 40-user run. CPU was never close to
    binding; the multiplier was just guessing, and guessing wrong in the
    same direction twice is a sign the axis itself doesn't belong here. The
    two real limits now: usable RAM (a genuine OOM risk on a swapless box,
    not something to estimate away) and HARD_CAP (the point past which
    Google's own per-user quota binds regardless of what the hardware could
    otherwise sustain -- the actual API limit this is meant to respect).
    """
    r = r or probe()

    # Share the machine when more than one migration is running.
    #
    # Every number below is derived from usable RAM on the assumption that
    # this job owns the box -- which is exactly why job_admission capped
    # concurrency at one. Two tenants each sizing a full pool against the
    # same 2.8 GB is not two migrations, it is the swap stall this module
    # exists to prevent, twice. Dividing the budget is what makes raising
    # that cap safe rather than optimistic.
    #
    # Ceil, not floor, on the divisor: three jobs on a nine-worker box get
    # three each, and a box that can only support one worker still gets one
    # per job rather than zero.
    jobs = max(1, int(concurrent_jobs))
    budget_mb = (r.ram_usable_gb * 1024) / jobs

    by_ram = int(budget_mb // MB_PER_WORKER)
    workers = max(MIN_WORKERS, min(by_ram, HARD_CAP))

    why = []
    if r.under_memory_pressure:
        # More concurrency here does not go faster, it goes to swap. This is
        # the case that produced 30 minutes of socket timeouts.
        workers = MIN_WORKERS
        why.append(
            f"machine is under memory pressure "
            f"({r.ram_usable_gb:.1f} GB usable, swap {r.swap_fraction * 100:.0f}% used) "
            f"— holding at {MIN_WORKERS} worker to avoid swap stalls that surface "
            f"as socket timeouts"
        )
    elif by_ram < HARD_CAP:
        why.append(f"memory-bound: {budget_mb / 1024:.1f} GB budget"
                   + (f" ({r.ram_usable_gb:.1f} GB usable / {jobs} concurrent "
                      f"jobs)" if jobs > 1 else " usable")
                   + " / "
                   f"{MB_PER_WORKER} MB per worker = {by_ram}")
    else:
        # Say the split happened even when it was not the binding constraint.
        #
        # Once MB_PER_WORKER dropped to its real measured value, RAM stopped
        # binding on any ordinary box and this branch started answering every
        # question -- including "did my second tenant halve the first one's
        # pool?" -- with a sentence about Google's quotas that never mentions
        # sharing at all. The split is still being applied; it just stopped
        # being what decided the number, and those are different facts.
        why.append(
            f"capped at {HARD_CAP}; each worker owns a different user, so "
            f"what binds past this is the per-project quota, which the "
            f"adaptive limiter paces")
        if jobs > 1:
            why.append(f"{budget_mb / 1024:.1f} GB budget "
                       f"({r.ram_usable_gb:.1f} GB usable / {jobs} concurrent jobs) "
                       f"would have allowed {by_ram}")

    return {
        "user_workers": workers,
        # Sized independently of user_workers -- see MB_PER_SEED_WORKER and
        # SEED_HARD_CAP for why both of that figure's inputs are wrong for
        # the seeder. Still held at MIN_WORKERS under real memory pressure:
        # a swapping box is slower at any concurrency, whatever the job.
        "seed_workers": (
            MIN_WORKERS if r.under_memory_pressure
            else max(MIN_WORKERS, min(
                int(budget_mb // MB_PER_SEED_WORKER),
                SEED_HARD_CAP))
        ),
        # Requests/sec per user. Was 8.0/4.0 -- Google's own Drive migration
        # guidance recommends sustained writes around 3 requests/sec/account;
        # 8 ran hotter than that, spending Drive's per-user write quota on
        # requests that just come back as 429/rateLimitExceeded and get
        # retried anyway. 3.0 tracks the documented rate directly; halved
        # under memory pressure for the same reason the worker count drops
        # then -- a machine already struggling should not also be racing to
        # fill a retry queue.
        "per_user_qps": 3.0 if not r.under_memory_pressure else 1.5,
        # Concurrent file copies *inside* one user. 4 is where the write
        # ceiling binds: ~3 target-account writes per file against 3/sec
        # needs about that many in flight to keep the limiter fed while each
        # waits on its round trip.
        #
        # Halved under memory pressure because in download_upload mode this
        # multiplies: peak buffer is user_workers x drive_file_workers x
        # download_chunk_bytes, and a machine already swapping is exactly the
        # one that turns extra buffers into the socket timeouts this module
        # exists to prevent. server_side streams no bytes through the host
        # and does not care, but the budget has to hold for the worse mode.
        "drive_file_workers": 4 if not r.under_memory_pressure else 2,
        # Same shape as drive_file_workers: concurrency inside one user,
        # halved when the box is already trading memory for disk.
        "mail_workers": 4 if not r.under_memory_pressure else 2,
        "reason": "; ".join(why),
        # The seed pool is sized on different inputs (MB_PER_SEED_WORKER,
        # SEED_HARD_CAP), so it needs its own sentence. Reusing `reason`
        # printed the self-contradicting "Workers: 32 (memory-bound: ... = 9)"
        # -- the count from one model, the explanation from the other.
        "seed_reason": (
            why[0] if r.under_memory_pressure else
            (f"memory-bound: {budget_mb / 1024:.1f} GB budget"
             + (f" ({r.ram_usable_gb:.1f} GB usable / {jobs} concurrent jobs)"
                if jobs > 1 else " usable")
             + " / "
             f"{MB_PER_SEED_WORKER} MB per seed worker = "
             f"{int((r.ram_usable_gb * 1024) // MB_PER_SEED_WORKER)}"
             if int((r.ram_usable_gb * 1024) // MB_PER_SEED_WORKER) < SEED_HARD_CAP
             else f"capped at {SEED_HARD_CAP}; each seeded user is a separate "
                  f"account with its own write ceiling, so the per-project "
                  f"quota binds before the per-user one")),
        "resources": r,
    }


def describe() -> str:
    rec = recommend()
    r: SystemResources = rec["resources"]
    where = r.platform + (" (cgroup-limited)" if r.container_limited else "")
    swap = (f"{r.swap_used_gb:.1f} / {r.swap_total_gb:.1f} GB "
            f"({r.swap_fraction * 100:.0f}% used)"
            if r.swap_total_gb else
            "none — no spill space, so memory exhaustion is an OOM kill")
    # Built before the list rather than inline: nested quotes inside an
    # f-string expression need 3.12+, and the VPS builds its venv from the
    # host python, which is 3.10.
    fds = _FD_LIMIT_RESULT
    if fds is None:
        fd_line = "not tunable on this platform"
    elif fds[1] > fds[0]:
        fd_line = f"{fds[0]} -> {fds[1]} (soft limit raised)"
    else:
        fd_line = f"{fds[0]} (already at the hard limit)"

    lines = [
        f"  platform      {where}",
        f"  cores         {r.cpu_physical} physical / {r.cpu_logical} logical",
        f"  memory        {r.ram_total_gb:.1f} GB total, "
        f"{r.ram_usable_gb:.1f} GB usable without swapping",
        f"  swap          {swap}",
        f"  open files    {fd_line}",
        "",
        f"  workers       {rec['user_workers']}",
        f"  per-user QPS  {rec['per_user_qps']}",
        f"  because       {rec['reason']}",
    ]
    for note in r.notes:
        lines.append(f"  note          {note}")
    if r.under_memory_pressure:
        lines += [
            "",
            "  This machine is already trading memory for disk. Seeding or",
            "  migrating will work, but slowly, and adding workers makes it",
            "  worse rather than faster. Closing other applications is worth",
            "  more here than any setting.",
        ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
