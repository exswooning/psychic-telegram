"""
tests/test_webui_render.py
==========================
Does a poll rebuild the page?

The wizard re-checks state every few seconds. The first version rewrote
`main.innerHTML` on every one of those, which cleared the step-2 form and threw
away keyboard focus mid-word -- the form was unusable. The fix is a render
signature: rebuild only when what is on screen would actually differ.

That is a claim about behaviour, so it is tested by behaviour: the page's own
JavaScript is executed in node against a stub DOM, and the rebuilds are
counted. Asserting on the source text would not have caught the original bug,
because the source always *looked* correct.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

import webui

HARNESS = r"""
// --- minimal DOM ------------------------------------------------------
let RENDERS = {};                       // element id -> innerHTML write count
const els = {};
function mk(id){
  return {
    id: id, _html: '', _text: '', value: '', checked: false,
    className: '', disabled: false, style: {},
    set innerHTML(v){ RENDERS[this.id]=(RENDERS[this.id]||0)+1; this._html=v; },
    get innerHTML(){ return this._html; },
    set textContent(v){ this._text=v; }, get textContent(){ return this._text; },
    focus(){}, setSelectionRange(){}, scrollTop:0, scrollHeight:0,
    contains(){ return false; },
  };
}
const document = {
  getElementById: id => (els[id] = els[id] || mk(id)),
  querySelectorAll: () => [],
  activeElement: null,
};
const window = {open(){}};
const navigator = {clipboard:{writeText(){ return Promise.resolve(); }}};
const alert = () => {}, confirm = () => true, prompt = () => '';
const setTimeout = () => 0, setInterval = () => 0;
const fetch = () => new Promise(() => {});   // never resolves: no side effects

__PAGE_JS__

// --- scenario ---------------------------------------------------------
const steps = [];
for (let n = 1; n <= 9; n++)
  steps.push({n:n, title:'Step '+n, state:n===1?'done':'todo', note:'note '+n,
              help:['line'], auto:'', manual:false, actions:[]});
S = {steps:steps, total:9, done:1, migrated:0, failed:0,
     users_done:0, users_total:0, env:{}};
oauth = {auth_mode:'key', configured:false};
dwd = {tenants:[]};
acts = {};
cur = 2; follow = false;

const out = {};

draw();                       out.first = RENDERS['main']||0;
draw(); draw(); draw();       out.after_three_more_polls = RENDERS['main']||0;

// typing must not trigger a rebuild
document.getElementById('f-sd').value = 'c.example.com';
draw(); draw();               out.after_typing = RENDERS['main']||0;

// a real state change must
S.steps[1].state = 'done';
draw();                       out.after_state_change = RENDERS['main']||0;

// so must moving to another step
cur = 5; draw();              out.after_step_change = RENDERS['main']||0;

// and an explicit force
draw(true);                   out.after_force = RENDERS['main']||0;

// live counters update without a rebuild
S.migrated = 4571; S.failed = 3;
draw();                       out.after_counter_change = RENDERS['main']||0;

// The requirements screen must carry the delegation strings for whichever
// path was chosen -- that grant is the one step no software can perform.
ups = {source_key:{present:true,valid:true,path:'./keys/source-sa.json',
        detail:{client_email:'src-sa@p.iam.gserviceaccount.com',
                client_id:'114344169573197353518',project_id:'p'}},
       target_key:{present:false,valid:false},
       oauth_client:{present:false,valid:false}};
authMode='key';
authModes={key:{label:'Service-account key',blurb:'b',needs:['source_key','target_key']}};
runMode='migrate_only';
runModes={migrate_only:{label:'Migrate only',blurb:'b',
                        requires:[2,3,4,5],runs:['migrate']}};
dwd={tenants:[{side:'source',domain:'c.example.com',admin:'info@c.example.com',
               client_id:'114344169573197353518',
               scopes:'https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/gmail.readonly',
               scope_list:['a','b']},
              {side:'target',domain:'a.example.com',admin:'info@a.example.com',
               client_id:'9',scopes:'https://www.googleapis.com/auth/drive',scope_list:['a']}],
      seed:{combined:'https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/gmail.insert',
            combined_list:['a','b']}};
// step 5 outstanding, so its body renders
S.steps[4].state='manual';
view='require'; draw(true);
out.requirements = document.getElementById('main').innerHTML;

// and the seeding path must show the WIDER source line, not the read-only one
runMode='seed_and_migrate';
runModes.seed_and_migrate={label:'Seed, then migrate',blurb:'b',
                           requires:[2,3,4,5],runs:['seed','migrate']};
draw(true);
out.requirements_seed = document.getElementById('main').innerHTML;
view='run'; draw(true);
out.runscreen = document.getElementById('main').innerHTML;
view='path'; draw(true);

// a transient bad poll must not blow the panel away
const good = S;
out.before_error = RENDERS['main']||0;
S = {error:'boom'};
draw();                       out.after_transient_error = RENDERS['main']||0;
S = good;
out.mig_text = document.getElementById('s-mig').textContent;
out.fail_text = document.getElementById('s-fail').textContent;

console.log(JSON.stringify(out));
"""


def _run_harness() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed; cannot execute the page JavaScript")
    js = re.search(r"<script>(.*?)</script>", webui.PAGE, re.S).group(1)
    # The page's bootstrap line fires network calls on load; the stub fetch
    # never resolves, so nothing happens, but drop the timers anyway.
    src = HARNESS.replace("__PAGE_JS__", js)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        p = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, p.stderr[-2000:]
        return json.loads(p.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


@pytest.fixture(scope="module")
def r():
    return _run_harness()


class TestRenderThrottling:
    def test_first_draw_renders(self, r):
        assert r["first"] == 1

    def test_repeated_polls_do_not_rebuild_the_panel(self, r):
        """The actual bug: three more polls with identical state used to mean
        three more rebuilds, each one clearing the form."""
        assert r["after_three_more_polls"] == 1

    def test_typing_does_not_trigger_a_rebuild(self, r):
        """Form contents must not be part of the render signature, or every
        keystroke would race the next poll."""
        assert r["after_typing"] == 1

    def test_a_real_state_change_does_rebuild(self, r):
        """Throttling must not go so far that the wizard stops updating."""
        assert r["after_state_change"] == 2

    def test_changing_step_rebuilds(self, r):
        assert r["after_step_change"] == 3

    def test_force_always_rebuilds(self, r):
        assert r["after_force"] == 4

    def test_a_transient_error_does_not_destroy_the_panel(self, r):
        """A failed poll used to replace the step with an error box, taking
        anything typed into the form with it. Once something good has been
        rendered, a bad poll is ignored."""
        assert r["after_transient_error"] == r["before_error"], (
            "a failed poll rebuilt (and cleared) the panel")

    def test_the_requirements_screen_carries_the_delegation_strings(self, r):
        """That grant is the one step no software can perform, so the Client ID
        and the exact scope line belong where it is asked for."""
        html = " ".join(r["requirements"].split())
        assert "114344169573197353518" in html
        assert "Manage Domain Wide Delegation" in html
        assert "drive.readonly" in html
        assert "info@c.example.com" in html          # which admin to sign in as

    def test_a_seeding_path_is_shown_the_wider_source_line(self, r):
        """Seeding writes; migrating reads. The console editor replaces rather
        than appends, so a seed path shown the read-only line would paste a
        grant that cannot seed."""
        assert "gmail.insert" in r["requirements_seed"]

    def test_the_run_screen_offers_the_paths_own_steps(self, r):
        assert "Live output" in r["runscreen"]

    def test_requirements_show_the_scopes_for_the_matching_tenant(self, r):
        """source must not be handed the target's scope line -- pasting write
        scopes into the source console is worse than useless."""
        html = " ".join(r["requirements"].split())
        assert "gmail.readonly" in html              # source set
        assert "gmail.insert" not in html            # target-only scope

    def test_counters_update_without_rebuilding(self, r):
        """During a migration these change constantly. Rebuilding on each would
        reset the output pane's scroll position every few seconds."""
        assert r["after_counter_change"] == 4
        assert r["mig_text"] == "4,571"
        assert r["fail_text"] == "3"
