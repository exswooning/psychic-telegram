#!/usr/bin/env python3
"""Ask the coordinator what to do, and do it. Runs ON a worker node.

The Nodes page has a Start button now, and this is what makes that possible
without the coordinator ever opening a connection to a machine. The button
writes desired state into node_directives; this polls for it and acts.
Pulled, never pushed -- fleet_agent.py's reasoning applies unchanged: a
control plane that could reach into its nodes would need credentials for
every machine holding service-account keys for both tenants, which turns a
dashboard into a lateral-movement path across the whole migration.

    python node_agent.py               # settings from node.env beside it

Stopping is cooperative. A directive going to 0 sends SIGINT, which main.py
handles by finishing the user it is on and then exiting -- killing it
outright would strand a mailbox half-copied, and on Drive would leave items
the local ledger has not recorded, which is the one thing a resume cannot
recover from.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
POLL_S = 20


def _load_env(path: str) -> None:
    """node.env, in the same KEY=value shape both installers write."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _get(url: str, token: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"X-Node-Token": token})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def _post(url: str, token: str, payload: dict, timeout: float = 20.0) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"X-Node-Token": token, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=timeout).read()


class Agent:
    def __init__(self, coordinator: str, token: str, node_id: str,
                 account_id: int, python: str = sys.executable,
                 workdir: str = HERE):
        self.coordinator = coordinator.rstrip("/")
        self.token, self.node_id = token, node_id
        self.account_id, self.python, self.workdir = account_id, python, workdir
        self.proc: subprocess.Popen | None = None
        self.services = ""
        self._specs: dict | None = None

    # -- the two things it does ------------------------------------------
    def directive(self) -> dict:
        # node_id, so the answer can be about THIS machine: the operator can
        # exclude one laptop from a run without stopping the run.
        import urllib.parse
        return _get(f"{self.coordinator}/api/v2/nodes/directive"
                    f"?account_id={self.account_id}"
                    f"&node_id={urllib.parse.quote(self.node_id)}", self.token)

    def specs(self) -> dict:
        """What this machine is. Measured once and cached: cores and RAM do
        not change between polls, and probing them every 20 seconds would
        spend real work to re-learn the same answer."""
        if self._specs is not None:
            return self._specs
        out: dict = {}
        try:
            import resources
            r = resources.probe()
            out["cpu_cores"] = r.cpu_logical or None
            out["ram_gb"] = round(r.ram_total_gb, 1) or None
            out["platform"] = r.platform or None
        except Exception:      # noqa: BLE001 - specs are not worth a failure
            pass
        try:
            st = os.statvfs(self.workdir)
            out["disk_gb"] = round(st.f_blocks * st.f_frsize / 1e9, 1)
        except Exception:      # noqa: BLE001 - Windows has no statvfs
            try:
                import shutil
                out["disk_gb"] = round(shutil.disk_usage(self.workdir).total / 1e9, 1)
            except Exception:      # noqa: BLE001
                pass
        self._specs = out
        return out

    def load(self) -> dict:
        """How busy it is right now. Separate from specs because these do
        change every poll, and because a failed measurement here must send
        null rather than zero -- upsert_node leaves a stored value alone for
        None, and 0.0 would read as "idle" on a node that is flat out."""
        out: dict = {}
        try:
            import resources
            r = resources.probe()
            if r.ram_total_gb:
                out["ram_pct"] = round((1 - r.ram_usable_gb / r.ram_total_gb) * 100, 1)
        except Exception:      # noqa: BLE001
            pass
        try:
            load1 = os.getloadavg()[0]
            out["cpu_pct"] = round(min(100.0, load1 / max(os.cpu_count() or 1, 1) * 100), 1)
        except Exception:      # noqa: BLE001 - no getloadavg on Windows
            pass
        try:
            import shutil
            u = shutil.disk_usage(self.workdir)
            out["disk_pct"] = round(u.used / u.total * 100, 1)
        except Exception:      # noqa: BLE001
            pass
        return out

    def heartbeat(self) -> None:
        """Best effort. A coordinator that cannot be told is not a reason to
        stop migrating -- the claim calls are what must not be guessed at,
        and main.py already stops on its own if those fail."""
        running = self.proc is not None and self.proc.poll() is None
        try:
            body = {
                "node_id": self.node_id,
                "hostname": self.node_id,
                "active_job": (f"migrate {self.services or 'all'}"
                               if running else None),
                "job_pid": self.proc.pid if running else None,
            }
            body.update(self.specs())
            body.update(self.load())
            _post(f"{self.coordinator}/api/v2/fleet/heartbeat", self.token, body)
        except Exception:      # noqa: BLE001 - reported by absence instead
            pass

    # -- the child -------------------------------------------------------
    def start(self, services: str) -> None:
        argv = [self.python, "main.py", "--account-id", str(self.account_id),
                "migrate"]
        if services:
            argv += ["--services", services]
        log = os.path.join(self.workdir, "node_agent_run.log")
        self.services = services
        # start_new_session so a restart of this agent does not take the
        # migration with it. POSIX only; on Windows it is ignored, and a
        # migration there does not survive the agent, which is worth knowing
        # before running a long one on a laptop.
        kwargs = {}
        if hasattr(os, "setsid"):
            kwargs["start_new_session"] = True
        with open(log, "a", encoding="utf-8") as fh:
            self.proc = subprocess.Popen(argv, cwd=self.workdir, stdout=fh,
                                         stderr=subprocess.STDOUT, **kwargs)
        print(f"  started pid {self.proc.pid}: {' '.join(argv)}", flush=True)

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        print(f"  stopping pid {self.proc.pid} (SIGINT)", flush=True)
        try:
            # SIGINT, not SIGKILL: main.py finishes the user it is on.
            # SIGKILL would strand a mailbox half-copied and, on Drive,
            # leave items the local ledger never recorded.
            self.proc.send_signal(getattr(signal, "SIGINT", signal.SIGTERM))
        except Exception:      # noqa: BLE001 - already gone is fine
            pass

    def tick(self) -> str:
        running = self.proc is not None and self.proc.poll() is None
        try:
            d = self.directive()
        except Exception as exc:      # noqa: BLE001
            return f"coordinator unreachable ({str(exc)[:60]})"
        want = bool(d.get("run"))
        # Both wrapped. An exception escaping here escapes the loop in
        # main() too, and the agent exits -- leaving the node silently
        # offline with no process left to say why. Anything that can fail
        # once (a bad path, a full disk, a permission) would otherwise end
        # the machine's participation permanently rather than for one cycle.
        if want and not running:
            try:
                self.start(str(d.get("services") or ""))
                return "started"
            except Exception as exc:      # noqa: BLE001
                return f"could not start ({str(exc)[:70]})"
        if not want and running:
            try:
                self.stop()
                return "stopping"
            except Exception as exc:      # noqa: BLE001
                return f"could not stop ({str(exc)[:70]})"
        if running:
            return "running"
        return "idle"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=os.path.join(HERE, "node.env"))
    ap.add_argument("--poll", type=float, default=POLL_S)
    ap.add_argument("--once", action="store_true",
                    help="one cycle and exit -- for checking it is wired up")
    args = ap.parse_args(argv)

    _load_env(args.env)
    coordinator = os.getenv("BITPORT_COORDINATOR", "").strip()
    token = os.getenv("BITPORT_NODE_TOKEN", "").strip()
    node_id = os.getenv("BITPORT_NODE_ID", "").strip() or os.uname().nodename \
        if hasattr(os, "uname") else os.getenv("COMPUTERNAME", "node")
    account = os.getenv("BITPORT_ACCOUNT", "").strip()
    if not coordinator or not token:
        sys.exit(f"no coordinator or token in {args.env} -- "
                 "run the installer, or set BITPORT_COORDINATOR and "
                 "BITPORT_NODE_TOKEN")
    if not account:
        sys.exit(f"no BITPORT_ACCOUNT in {args.env} -- a node works on one "
                 "tenant and has to be told which")

    agent = Agent(coordinator, token, node_id, int(account))
    print(f"node {node_id} -> {agent.coordinator}, account {account}")
    while True:
        state = agent.tick()
        agent.heartbeat()
        print(f"  {time.strftime('%H:%M:%S')}  {state}", flush=True)
        if args.once:
            return 0
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
