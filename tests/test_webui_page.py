"""
tests/test_webui_page.py
========================
The served page.

`webui.PAGE` is a Python string containing JavaScript, so every backslash is
escaped twice. Get it wrong and the result is a *blank page* -- no server
error, no failing request, nothing in the log. Caught live: a `\\n` intended as
a JS newline escape became a real newline inside a quoted string, and the whole
UI stopped rendering while every server-side test still passed.

Deliberately in its own file: tests/test_wizard.py patches `wizard.shutil.which`
for gcloud isolation, and that is the *same module object* as `shutil`, so an
autouse fixture there silently disables the node lookup here and the parse
check skips instead of running.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import pytest

import webui


def _js() -> str:
    m = re.search(r"<script>(.*?)</script>", webui.PAGE, re.S)
    assert m, "no <script> block in PAGE"
    return m.group(1)


class TestServedPage:
    def test_javascript_parses(self):
        """The only reliable check -- a real JS parser. Everything short of
        this is a heuristic that either misses the bug or flags a regex
        character class as an unterminated string."""
        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed; cannot parse-check the page")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(_js())
            path = fh.name
        try:
            p = subprocess.run([node, "--check", path],
                               capture_output=True, text=True)
            assert p.returncode == 0, p.stderr
        finally:
            os.unlink(path)

    def test_page_has_exactly_one_script_block(self):
        assert webui.PAGE.count("<script>") == webui.PAGE.count("</script>") == 1

    def test_the_controls_the_wizard_depends_on_are_present(self):
        """Cheap structural guard for machines with no node: the handlers the
        buttons call must exist, or the step renders with dead controls."""
        js = _js()
        # The three-screen flow: pick a path, satisfy its requirements, run it.
        for fn in ("function draw(", "function drawRail(",
                   "function screenPath(", "function screenRequire(",
                   "function screenRun(", "function requirementBody(",
                   "function delegationBody(", "function credentialsBody(",
                   "function configForm(", "function restoreForm(",
                   "async function saveCfg(", "async function runSeed(",
                   "async function run(", "async function refresh(",
                   "async function pollJob("):
            assert fn in js, f"missing {fn}"

    def test_every_onclick_names_a_function_that_exists(self):
        """An onclick pointing at a typo'd name fails only when clicked."""
        js = _js()
        called = set(re.findall(r"onclick=\\?\"([a-zA-Z_$][\w$]*)\(", webui.PAGE))
        called |= set(re.findall(r"onclick=\"([a-zA-Z_$][\w$]*)\(", webui.PAGE))
        for name in called:
            if name in ("document", "window"):
                continue
            assert (f"function {name}(" in js or f"async function {name}(" in js
                    or f"{name}=" in js), f"onclick calls undefined {name}()"

    def test_no_external_resources(self):
        """The page must work on an air-gapped migration host."""
        for bad in ("http://cdn", "https://cdn", "<script src=", "@import",
                    "fonts.googleapis"):
            assert bad not in webui.PAGE, f"external resource: {bad}"

    def test_tabs_are_wired_to_settab(self):
        """Caught live: the tab buttons rendered but nothing bound their
        clicks, so Dashboard/Users/Failures/Scope/Logs/Output were dead."""
        js = _js()
        assert "b.onclick=()=>setTab(b.dataset.tab)" in js
        for label in ("Setup", "Dashboard", "Users", "Failures", "Scope",
                      "Logs", "Output"):
            assert f'data-tab="{label.lower()}"' in webui.PAGE, label

    def test_header_progress_strip_present(self):
        """The always-visible progress bar under the header + its updater."""
        assert 'id="progi"' in webui.PAGE and 'id="progpct"' in webui.PAGE
        assert 'function paintProg()' in _js()
