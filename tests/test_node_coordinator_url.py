"""A node aimed at :8090 gets connection refused.

api_server.py binds 127.0.0.1 by default -- deliberately, and it warns
loudly if you pass --host (see its own module docstring). On a standard
install.sh deployment, confirmed live:

    LISTEN 127.0.0.1:8090   python      <- api_server.py
    LISTEN         *:80     caddy       <- the only thing facing the network

So 8090 is reachable from the coordinator itself and nowhere else. Every
worker-node example in this repo used to say `--coordinator http://HOST:8090`,
which cannot work from another machine. Caddy proxies /api/v2/* through,
and /api/v2/claims/... is the only path user_claims.py ever calls, so the
Caddy port is the right answer and the loopback port is never one.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ("install_node.sh", "install_node.ps1", "node_setup.sh", "MULTINODE.md")


def _read(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def test_no_example_points_a_node_at_the_loopback_port():
    """Matches a host:8090 URL, not the bare number -- the files explain
    WHY 8090 is wrong, and that prose must stay allowed."""
    url = re.compile(r"https?://[^\s\"'`]+:8090")
    for name in DOCS:
        hits = url.findall(_read(name))
        assert not hits, f"{name} still aims a node at the loopback port: {hits}"


def test_each_one_says_which_port_to_use_instead():
    """Removing the wrong port without naming the right one just moves the
    hour someone loses from debugging to guessing."""
    for name in DOCS:
        body = _read(name)
        assert "Caddy" in body or "CADDY" in body, \
            f"{name} drops 8090 but never names the port that works"
        assert "127.0.0.1" in body, \
            f"{name} must say why -- api_server binds loopback only"


def test_the_claim_path_really_is_proxied_by_caddy():
    """The advice above is only true while Caddy routes that prefix. If the
    Caddyfile stops proxying /api/v2/*, pointing nodes at it silently
    becomes wrong again."""
    caddy = _read("Caddyfile")
    assert "reverse_proxy /api/v2/* 127.0.0.1:8090" in caddy
    claims = _read("user_claims.py")
    assert "/api/v2/claims/" in claims


def test_api_server_still_defaults_to_loopback():
    """The whole reason for the above. If this ever defaults to 0.0.0.0 the
    guidance needs rewriting -- and so does the security review that made it
    loopback in the first place."""
    src = _read("api_server.py")
    assert 'ap.add_argument("--host", default="127.0.0.1")' in src
