"""A library upgrade must never restart a running migration.

13 Nov 2026, account 3: an unattended upgrade re-executed systemd and needrestart then restarted every
bitport-* unit at 06:54 UTC -- the DMS import was stopped mid-flight, started again during the network
blip the same upgrade caused, and died on a DNS error. Units are restarted by a deploy, on an idle box."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_rule_exempts_every_bitport_unit():
    conf = open(os.path.join(ROOT, "systemd", "needrestart-bitport.conf")).read()
    rule = re.search(r"^\$nrconf\{override_rc\}\{qr\((.+?)\)\}\s*=\s*0;", conf, re.M)
    assert rule, "the override must switch restarts OFF (= 0)"
    pattern = re.compile(rule.group(1))
    for unit in ("bitport-api.service", "bitport-webui.service", "bitport-dms-fresh.service",
                 "bitport-parity-r3.service", "bitport-autopilot.service"):
        assert pattern.search(unit), unit
    assert not pattern.search("ssh.service"), "only Bitport's own units are exempt"


def test_a_deploy_installs_the_rule():
    script = open(os.path.join(ROOT, "sync_vps.sh")).read()
    assert "needrestart-bitport.conf" in script and "/etc/needrestart/conf.d/bitport.conf" in script
