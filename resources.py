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
from dataclasses import dataclass, field

# Peak resident memory a single worker needs, in MB. Derived from the migrator
# streaming a file through memory plus the discovery document and client
# objects each thread holds. Generous on purpose: under-estimating it is what
# produces the swap-stall failure this module exists to prevent.
MB_PER_WORKER = 320

# Above this fraction of swap in use, the machine is already trading disk for
# memory and more concurrency makes it strictly worse.
SWAP_DISTRESS = 0.60

HARD_CAP = 16          # beyond this, Google's per-user quotas bind first
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
    notes: list[str] = field(default_factory=list)

    @property
    def swap_fraction(self) -> float:
        return (self.swap_used_gb / self.swap_total_gb) if self.swap_total_gb else 0.0

    @property
    def under_memory_pressure(self) -> bool:
        return self.swap_fraction >= SWAP_DISTRESS or self.ram_usable_gb < 1.0


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
    r.cpu_logical = os.cpu_count() or 1
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
        return
    r.ram_total_gb = info.get("MemTotal", 0.0)
    # MemAvailable is the kernel's own estimate and better than free+cached.
    r.ram_usable_gb = info.get("MemAvailable", info.get("MemFree", 0.0))
    r.swap_total_gb = info.get("SwapTotal", 0.0)
    r.swap_used_gb = r.swap_total_gb - info.get("SwapFree", 0.0)


def probe() -> SystemResources:
    r = SystemResources(platform=sys.platform)
    try:
        if sys.platform == "darwin":
            _probe_macos(r)
        elif sys.platform.startswith("linux"):
            _probe_linux(r)
        else:
            r.cpu_logical = r.cpu_physical = os.cpu_count() or 1
            r.notes.append(f"no memory probe for {sys.platform}; assuming 4 GB usable")
            r.ram_total_gb = r.ram_usable_gb = 4.0
    except Exception as exc:  # noqa: BLE001
        r.notes.append(f"probe failed ({exc}); using conservative defaults")
    if r.cpu_logical < 1:
        r.cpu_logical = os.cpu_count() or 1
    return r


def recommend(r: SystemResources | None = None) -> dict:
    """
    Worker counts this machine can actually sustain.

    Memory is the binding constraint, not CPU: the pools are I/O-bound, so the
    cap that matters is how many in-flight buffers fit without swapping.
    """
    r = r or probe()

    by_ram = int((r.ram_usable_gb * 1024) // MB_PER_WORKER)
    by_cpu = max(r.cpu_logical, 1) * 2          # I/O-bound: oversubscribe cores
    workers = max(MIN_WORKERS, min(by_ram, by_cpu, HARD_CAP))

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
    elif by_ram < by_cpu:
        why.append(f"memory-bound: {r.ram_usable_gb:.1f} GB usable / "
                   f"{MB_PER_WORKER} MB per worker = {by_ram}")
    else:
        why.append(f"cpu-bound: {r.cpu_logical} logical cores x2 = {by_cpu}")
    if workers == HARD_CAP:
        why.append(f"capped at {HARD_CAP}; past this Google's per-user quotas bind first")

    return {
        "user_workers": workers,
        # The seeder holds a whole corpus per user; give it the same ceiling.
        "seed_workers": workers,
        # Requests/sec per user. Raising this on a machine that cannot keep up
        # just fills the retry queue.
        "per_user_qps": 8.0 if not r.under_memory_pressure else 4.0,
        "reason": "; ".join(why),
        "resources": r,
    }


def describe() -> str:
    rec = recommend()
    r: SystemResources = rec["resources"]
    lines = [
        f"  platform      {r.platform}",
        f"  cores         {r.cpu_physical} physical / {r.cpu_logical} logical",
        f"  memory        {r.ram_total_gb:.1f} GB total, "
        f"{r.ram_usable_gb:.1f} GB usable without swapping",
        f"  swap          {r.swap_used_gb:.1f} / {r.swap_total_gb:.1f} GB "
        f"({r.swap_fraction * 100:.0f}% used)",
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
