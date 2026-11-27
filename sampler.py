"""Append CPU and network counter samples to JSONL while a job runs.

Linux host sampler for Claude's live Bitport runs. It uses /proc and sysfs,
has no third-party dependency, and appends one flushed JSON object per sample.
The first line carries counters; later lines also carry interval rates.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def read_cpu_counters(path: str = "/proc/stat") -> tuple[int, int]:
    """Return aggregate (total, idle+iowait) CPU ticks from Linux /proc/stat."""
    with open(path, encoding="ascii") as fh:
        first = fh.readline().split()
    if not first or first[0] != "cpu" or len(first) < 5:
        raise OSError(f"no aggregate CPU counters in {path}")
    values = [int(x) for x in first[1:]]
    # guest and guest_nice are already included in user and nice on Linux.
    values = values[:8]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def read_interface_counters(interface: str, root: str = "/sys/class/net") -> tuple[int, int]:
    """Return (rx_bytes, tx_bytes) for one Linux network interface."""
    base = Path(root) / interface / "statistics"
    try:
        rx = int((base / "rx_bytes").read_text(encoding="ascii").strip())
        tx = int((base / "tx_bytes").read_text(encoding="ascii").strip())
    except FileNotFoundError as exc:
        raise OSError(f"interface {interface!r} not found under {root}") from exc
    return rx, tx


class Sampler:
    def __init__(self, interface: str = "wlp2s0", *,
                 cpu_reader: Callable[[], tuple[int, int]] | None = None,
                 network_reader: Callable[[str], tuple[int, int]] | None = None,
                 clock: Callable[[], float] = time.time):
        self.interface = interface
        self.cpu_reader = cpu_reader or read_cpu_counters
        self.network_reader = network_reader or read_interface_counters
        self.clock = clock
        self.previous: dict | None = None

    def sample(self) -> dict:
        now = self.clock()
        total, idle = self.cpu_reader()
        rx, tx = self.network_reader(self.interface)
        prior = self.previous
        elapsed = max(0.0, now - prior["at"]) if prior else None
        busy_pct = None
        rx_rate = tx_rate = None
        if prior and elapsed and elapsed > 0:
            dt = total - prior["cpu_total"]
            di = idle - prior["cpu_idle"]
            if dt > 0 and 0 <= di <= dt:
                busy_pct = 100.0 * (dt - di) / dt
            if rx >= prior["rx"]:
                rx_rate = (rx - prior["rx"]) / elapsed
            if tx >= prior["tx"]:
                tx_rate = (tx - prior["tx"]) / elapsed
        self.previous = {"at": now, "cpu_total": total, "cpu_idle": idle, "rx": rx, "tx": tx}
        network = {"rx_bytes": rx, "tx_bytes": tx,
                   "rx_bytes_per_sec": rx_rate, "tx_bytes_per_sec": tx_rate,
                   "bytes_per_sec": (rx_rate + tx_rate) if rx_rate is not None and tx_rate is not None else None}
        return {"timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z"),
                "interface": self.interface,
                "interval_sec": elapsed,
                "cpu": {"total_ticks": total, "idle_ticks": idle, "busy_pct": busy_pct},
                "network": network}


def sample_to_file(output: str, *, interface: str = "wlp2s0", interval: float = 5.0,
                   count: int | None = None, sampler: Sampler | None = None,
                   sleep: Callable[[float], None] = time.sleep) -> int:
    """Append samples until interrupted or ``count`` samples have been written."""
    if interval <= 0:
        raise ValueError("interval must be greater than zero")
    sampler = sampler or Sampler(interface)
    parent = os.path.dirname(os.path.abspath(output))
    os.makedirs(parent, exist_ok=True)
    written = 0
    with open(output, "a", encoding="utf-8", buffering=1) as fh:
        while count is None or written < count:
            sample = sampler.sample()
            fh.write(json.dumps(sample, separators=(",", ":")) + "\n")
            fh.flush()
            written += 1
            if count is None or written < count:
                sleep(interval)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="wlp2s0")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between samples")
    parser.add_argument("--output", required=True, help="append-only JSONL destination")
    parser.add_argument("--count", type=int, help="stop after N readings; default runs until Ctrl-C")
    args = parser.parse_args(argv)
    try:
        n = sample_to_file(args.output, interface=args.interface, interval=args.interval, count=args.count)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        print(f"sampler: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {n} sample(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
