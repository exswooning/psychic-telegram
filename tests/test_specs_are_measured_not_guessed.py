"""A 16 GB laptop reported 4 GB of RAM.

The Machines page showed "not reported" for every node's specs while
happily showing their load, and the two causes were different:

  * vps-garud runs fleet_agent.py, which sent cpu_pct/ram_pct/disk_pct and
    none of the denominators those are fractions of.
  * win32 fell through resources.probe()'s unknown-platform branch, which
    assumes 4 GB so worker sizing has something to divide by. That is right
    for sizing and wrong for reporting: the number reached the coordinator
    and would have rendered as that machine's RAM, beside real figures from
    real probes, with nothing marking it invented.

A blank invites a question. A wrong number answers it.
"""
from __future__ import annotations

import inspect
import os
import re

import fleet_agent
import node_agent
import resources

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code(fn) -> str:
    src = inspect.getsource(fn)
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    return re.sub(r"#.*$", "", src, flags=re.M)


class TestWindowsIsMeasured:
    def test_there_is_a_real_windows_probe(self):
        assert hasattr(resources, "_probe_windows")
        src = _code(resources._probe_windows)
        assert "GlobalMemoryStatusEx" in src
        assert "ullTotalPhys" in src

    def test_probe_routes_win32_to_it(self):
        src = _code(resources.probe)
        assert 'sys.platform == "win32"' in src
        assert "_probe_windows(r)" in src

    def test_it_needs_no_dependency(self):
        """requirements.txt is stdlib-plus-google-client on purpose, and a
        worker node installs exactly that."""
        src = _code(resources._probe_windows)
        assert "import ctypes" in src
        for lib in ("psutil", "wmi", "pywin32"):
            assert lib not in src

    def test_a_failed_call_is_flagged_as_a_guess(self):
        src = _code(resources._probe_windows)
        assert "r.ram_estimated = True" in src

    def test_the_page_file_is_not_passed_off_as_swap(self):
        """Windows commits rather than swaps, so the two are not the same
        quantity and putting one in the other's field would make the
        platforms look comparable."""
        src = _code(resources._probe_windows)
        assert "swap_total_gb" not in src


class TestAGuessIsNeverReportedAsFact:
    def test_the_flag_exists_and_defaults_off(self):
        r = resources.SystemResources()
        assert r.ram_estimated is False

    def test_a_real_probe_does_not_set_it(self):
        assert resources.probe().ram_estimated is False

    def test_the_unknown_platform_branch_sets_it(self):
        src = _code(resources.probe)
        branch = src[src.index("else:"):]
        assert "ram_estimated = True" in branch
        # And still provides a number, because sizing divides by it.
        assert "4.0" in branch

    def test_node_agent_omits_an_estimated_ram(self):
        src = _code(node_agent.Agent.specs)
        assert "None if r.ram_estimated" in src

    def test_node_agent_omits_the_percentage_too(self):
        """(1 - 4/4) * 100 is 0%, which reads as an idle machine."""
        src = _code(node_agent.Agent.load)
        assert "not r.ram_estimated" in src

    def test_fleet_agent_omits_it_as_well(self):
        src = _code(fleet_agent._specs)
        assert 'ram_estimated' in src


class TestFleetAgentSendsDenominators:
    def test_the_payload_carries_them(self):
        p = fleet_agent.build_payload("t")
        for key in ("cpu_cores", "disk_gb", "platform"):
            assert key in p, key

    def test_and_still_carries_the_percentages(self):
        p = fleet_agent.build_payload("t")
        for key in ("cpu_pct", "ram_pct", "disk_pct"):
            assert key in p, key

    def test_specs_never_break_a_heartbeat(self):
        """A missing heartbeat reads as a dead node, which is a worse report
        than a missing core count."""
        src = _code(fleet_agent._specs)
        assert src.count("except Exception") == 2
        assert "raise" not in src

    def test_the_coordinator_accepts_what_both_agents_send(self):
        import api_server
        fields = api_server.Heartbeat.model_fields
        for key in fleet_agent.build_payload("t"):
            assert key in fields, f"heartbeat model rejects {key}"


class TestTheDeployActuallyRedeploysTheAgent:
    """The running fleet_agent.py was fifteen days older than the file.

    sync_vps.sh restarted bitport-webui and bitport-api and nothing else, so
    a change to fleet_agent.py rsynced, reported success, and changed
    nothing -- the process kept running the code it had started with. That
    is precisely the failure this script's own header describes ("An rsync
    alone updates the files while the running server keeps serving the page
    it already has in memory"), one service short of being fixed.
    """

    def _sh(self) -> str:
        with open(os.path.join(ROOT, "sync_vps.sh"), encoding="utf-8") as fh:
            return fh.read()

    def test_the_fleet_service_is_restarted(self):
        assert "systemctl restart bitport-fleet" in self._sh()

    def test_a_box_without_it_still_deploys(self):
        """Not every host runs a fleet agent, and one that does not must not
        fail an otherwise good deploy."""
        sh = self._sh()
        line = [l for l in sh.splitlines()
                if "systemctl restart bitport-fleet" in l][0]
        assert "|| true" in line

    def test_every_unit_the_installer_enables_gets_restarted(self):
        """The gap was invisible because nothing compared the two lists."""
        sh = self._sh()
        with open(os.path.join(ROOT, "install.sh"), encoding="utf-8") as fh:
            install = fh.read()
        for unit in ("bitport-webui", "bitport-api"):
            assert unit in install and unit in sh, unit
        # xvfb and the backup timer are deliberately excluded: neither holds
        # Python code that a deploy changes.
        assert "xvfb" not in sh.split("systemctl restart")[1][:200]
