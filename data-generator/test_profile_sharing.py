"""fableplan2 P4.2: the sharing of a profile run -- departments as groups, one shared mailbox, shared drives, the one outside
address, and link mail that names the real items. In-memory Drive and Gmail; no tenant, no network."""
import base64
import copy
import re

import pytest

import profile_plan as pp
import profile_seed
import seed_shared_drives as ssd
import seed_sandbox as seed
from test_profile_seed import PROFILE, _mini_profile, _NoLimiter, _Media, profile, _run  # noqa: F401
from tests.fakes import FakeDrive, FakeGmail

POOL_USER = "admin@tenanta.com"
OUTSIDE = pp.OUTSIDE_ADDRESS


class _Dir:
    """Directory: a second insert of a group or of a member is a 409, like Google's."""
    def __init__(self):
        self.groups_made, self.members_made, self._seen = [], [], set()

    def groups(self):
        return self

    def members(self):
        return self

    def insert(self, body=None, groupKey=None):
        key = (groupKey, (body or {}).get("email")) if groupKey else (body["email"],)
        if key in self._seen:
            raise RuntimeError("409 duplicate")
        self._seen.add(key)
        (self.members_made if groupKey else self.groups_made).append((groupKey, body))
        return type("R", (), {"execute": lambda s=None: {}})()


class _Delegates:
    def __init__(self):
        self.made = []

    def users(self):
        return self

    def settings(self):
        return self

    def delegates(self):
        return self

    def list(self, userId=None):
        return type("R", (), {"execute": lambda s=None: {"delegates": [{"delegateEmail": a} for a in self.made]}})()

    def create(self, userId=None, body=None):
        self.made.append(body["delegateEmail"])
        return type("R", (), {"execute": lambda s=None: {}})()


class World:
    """Every user's own Drive and Gmail, the pool visible to all (domain-shared in the real tenant), one directory."""

    def __init__(self, monkeypatch, settings):
        self.drives, self.gmails, self.settings_gmail, self.dir = {}, {}, {}, _Dir()
        self.pool_files: dict = {}
        monkeypatch.setattr(seed, "_media", _Media)
        monkeypatch.setattr(seed, "_retry_factory", lambda settings, limiter=None: (lambda fn: fn))
        monkeypatch.setattr(seed, "_seed_drive_limiter", lambda settings: _NoLimiter())
        monkeypatch.setattr(seed, "build_services", lambda settings, user: (self.drive(user), self.gmail(user), None))
        monkeypatch.setattr(seed, "build_chat", lambda settings, user: None)
        monkeypatch.setattr(seed, "build_directory_groups", lambda settings, user: self.dir)
        monkeypatch.setattr(seed, "build_gmail_settings", lambda settings, user: self.settings_gmail.setdefault(user, _Delegates()))
        monkeypatch.setattr(ssd, "_drive_client", lambda settings, user: self.drive(user))
        self.settings = settings

    def drive(self, user):
        if user not in self.drives:
            d = self.drives[user] = FakeDrive(user, user.split("@")[0])
            for fid, (meta, data) in self.pool_files.items():
                d.store[fid], d.content[fid] = copy.deepcopy(meta), data
        return self.drives[user]

    def gmail(self, user):
        return self.gmails.setdefault(user, FakeGmail(user, user.split("@")[0]))

    def pool(self, profile):
        pool = profile_seed.build_pool(seed, self.settings, POOL_USER, pp.file_mix(profile), 2_000_000, 40_000)
        src = self.drive(POOL_USER)
        self.pool_files = {fid: (src.store[fid], src.content.get(fid, b"")) for fid in pool["ids"]}
        self.drives.clear()
        return pool


@pytest.fixture
def world(monkeypatch, settings):
    return World(monkeypatch, settings)


N = 60


def _entries(profile, n=N, seed_=1):
    plans = pp.plan(_mini_profile(profile), n, seed_)
    sh = pp.sharing(plans, seed_)
    return plans, sh, seed._profile_entries(plans, "tenanta.com", sh)


def _share(world, profile):
    plans, sh, entries = _entries(profile)
    pool = world.pool(profile)
    return plans, sh, entries, pool, profile_seed.seed_sharing(seed, world.settings, entries, pool, OUTSIDE, sh, POOL_USER)


# ---- the plan --------------------------------------------------------------------------------------------------
def test_sharing_is_the_same_for_the_same_plan_and_seed_and_has_the_shape(profile):
    plans = pp.plan(profile, 300, 1)
    sh = pp.sharing(plans, 1)
    assert sh == pp.sharing(pp.plan(profile, 300, 1), 1) and sh != pp.sharing(plans, 2)
    assert set(sh["dept"]) <= set(pp.DEPARTMENTS) and len(set(sh["dept"])) > 4
    assert all(sh["dept"][i] == d and sh["dept"].index(d) == i for d, i in sh["lead"].items())    # the lead is the first member
    assert plans[sh["mailbox"]]["messages"] == max(p["messages"] for p in plans)
    assert len(sh["readers"]) == pp.SHARING["readers"] and sh["mailbox"] not in sh["readers"]
    assert len(sh["drives"]) == 12 and all(len(set(m)) == len(m) >= 36 for m in sh["drives"])
    assert 0.08 < len(sh["outside"]) / 300 < 0.25
    assert all(1 <= n <= 3 and plans[i]["files"] > plans[i]["shortcuts"] for i, n in sh["outside"].items())
    assert pp.sharing(plans[:1], 1)["readers"] == [] and pp.sharing(plans[:1], 1)["drives"] == []


# ---- departments, drives, mailbox ------------------------------------------------------------------------------
def test_departments_become_groups_and_their_folders_are_shared_with_them(world, profile):
    plans, sh, entries, pool, out = _share(world, profile)
    who = [e["email"] for e in entries]
    depts = {d for d in sh["lead"]}
    assert {b["name"] for _, b in world.dir.groups_made} == depts and out["groups"] == len(depts)
    assert len([m for m in world.dir.members_made if m[0].split("@")[0].startswith("dept-")]) == N
    for d, lead in sh["lead"].items():
        team = [who[i] for i, x in enumerate(sh["dept"]) if x == d]
        if len(team) < 2:
            assert not out["per_user"][team[0]]["colleague"]
            continue
        drive = world.drives[who[lead]]
        folder = next(f for f in drive.store.values() if f["name"] == f"Department {d}")
        kids = [f["id"] for f in drive.store.values() if folder["id"] in (f.get("parents") or [])]
        assert len(kids) == profile_seed.DEPT_FILES
        assert [p["emailAddress"] for p in drive.perms[folder["id"]]] == [f"dept-{profile_seed._slug(d)}@tenanta.com"]
        assert out["per_user"][who[lead]]["colleague"] == []                      # the lead owns them
        assert all(sorted(out["per_user"][u]["colleague"]) == sorted(kids) for u in team[1:])


def test_shared_drives_have_their_own_members_and_files_copied_from_the_pool(world, profile):
    plans, sh, entries, pool, out = _share(world, profile)
    who = [e["email"] for e in entries]
    admin = world.drives[POOL_USER]
    assert out["drives"] == len(sh["drives"]) == 2 and len(admin.shared_drives) == 2
    for n, ((did, _), idx) in enumerate(zip(admin.shared_drives.items(), sh["drives"]), 1):
        assert sorted(p["emailAddress"] for p in admin.perms[did] if p["type"] == "user") == sorted(who[i] for i in idx)
        files = [f for f in admin.store.values() if f["name"].startswith(f"sd{n}-")]
        assert len(files) == 8 and all(f["id"] in out["per_user"][who[i]]["shared_drive"] for f in files for i in idx)
    members = {i for idx in sh["drives"] for i in idx}
    assert all(bool(out["per_user"][who[i]]["shared_drive"]) == (i in members) for i in range(N))
    # a file in a drive is a server-side copy of a pool file, and nothing was uploaded for it
    assert admin.call_count("files.copy") >= 16 and not [c for c in admin.calls_to("files.create") if c["body"].get("parents") and
                                                         c["body"]["parents"][0] in admin.shared_drives and "media_body" in c]


def test_the_shared_mailbox_is_read_by_the_planned_readers(world, profile):
    plans, sh, entries, pool, out = _share(world, profile)
    who = [e["email"] for e in entries]
    assert out["mailbox"] == who[sh["mailbox"]] and out["readers"] == len(sh["readers"])
    assert sorted(world.settings_gmail[out["mailbox"]].made) == sorted(who[i] for i in sh["readers"])
    assert not out["notes"]


def test_sharing_again_adds_nothing(world, profile):
    plans, sh, entries, pool, first = _share(world, profile)
    snap = {u: (len(d.store), {k: len(v) for k, v in d.perms.items()}) for u, d in world.drives.items()}
    groups, members, delegates = len(world.dir.groups_made), len(world.dir.members_made), {u: list(g.made) for u, g in world.settings_gmail.items()}
    for d in world.drives.values():
        d.reset_calls()
    again = profile_seed.seed_sharing(seed, world.settings, entries, pool, OUTSIDE, sh, POOL_USER)
    assert snap == {u: (len(d.store), {k: len(v) for k, v in d.perms.items()}) for u, d in world.drives.items()}
    assert (groups, members) == (len(world.dir.groups_made), len(world.dir.members_made))
    assert delegates == {u: list(g.made) for u, g in world.settings_gmail.items()}
    assert not [n for d in world.drives.values() for n in ("files.create", "files.copy") if d.call_count(n)]
    assert again["per_user"] == first["per_user"] and again["readers"] == 0 and again["dept_files"] == 0 and not again["notes"]


def test_a_failure_in_one_step_is_reported_and_the_rest_still_runs(world, profile, monkeypatch):
    monkeypatch.setattr(seed, "build_gmail_settings", lambda settings, user: None)
    _, _, _, _, out = _share(world, profile)
    assert any("shared mailbox" in n for n in out["notes"]) and out["groups"] and out["drives"] == 2


# ---- the outside address ---------------------------------------------------------------------------------------
def _user(world, profile, i=0):
    plan = max(pp.plan(_mini_profile(profile), 3, 5), key=lambda p: p["files"])
    plan.update(messages=60, threads=50, link_mail=0, link_kinds=pp._split(0, pp.LINK_KINDS), labels=0)
    return plan, f"p{i}@tenanta.com"


def test_files_are_shared_outside_only_with_the_p06_address_and_only_once(world, profile):
    plan, user = _user(world, profile)
    pool = world.pool(profile)
    drive = world.drive(user)
    profile_seed.build_drive(seed, drive, user, plan, pool, lambda fn: fn)
    assert profile_seed.share_outside(seed, world.settings, drive, user, plan, 3, OUTSIDE, lambda fn: fn) == 3
    grants = [(fid, p) for fid, ps in drive.perms.items() for p in ps]
    assert len(grants) == 3 and {p["emailAddress"] for _, p in grants} == {OUTSIDE} and {p["type"] for _, p in grants} == {"user"}
    assert profile_seed.share_outside(seed, world.settings, drive, user, plan, 3, OUTSIDE, lambda fn: fn) == 0
    assert sum(len(v) for v in drive.perms.values()) == 3
    with pytest.raises(ValueError):
        profile_seed.share_outside(seed, world.settings, drive, user, plan, 3, "someone@gmail.com", lambda fn: fn)
    assert sum(len(v) for v in drive.perms.values()) == 3


def test_a_profile_run_refuses_any_other_outside_address(monkeypatch, capsys):
    monkeypatch.setenv("SEED_CAMPAIGN", "0")
    with pytest.raises(SystemExit) as e:
        seed.main(["--confirm-domain", "tenanta.com", "--profile", PROFILE, "--users", "9", "--plan-only", "--upload-mb-per-sec", "1",
                   "--external-email", "someone@gmail.com"])
    assert OUTSIDE in str(e.value)


def test_seed_one_user_shares_outside_once_then_marks_it_done(world, profile):
    plan, user = _user(world, profile)
    pool = world.pool(profile)
    entry = {"local": "p", "email": user, "dept": "Finance", "project": "P", "plan": plan, "pool": pool, "msg_bytes": 3000,
             "share": {"colleague": [], "shared_drive": [], "outside": 2}}
    run = lambda: seed.seed_one_user(seed.Settings(), entry, [user], OUTSIDE, "tiny", 10, 5, False, only=frozenset({"drive"}))  # noqa: E731
    run()
    drive = world.drives[user]
    assert "sharing" in profile_seed.done_services(drive, lambda fn: fn)
    assert sum(len(v) for v in drive.perms.values()) == 2
    drive.reset_calls()
    run()
    assert sum(len(v) for v in drive.perms.values()) == 2 and not drive.call_count("permissions.create")


# ---- link mail -------------------------------------------------------------------------------------------------
def test_link_mail_points_at_the_real_colleague_and_shared_drive_files(world, profile):
    plan, user = _user(world, profile)
    pool = world.pool(profile)
    plan.update(link_mail=20, link_kinds={"own": 0, "colleague": 10, "shared_drive": 10, "folder": 0, "deleted": 0, "outside": 0})
    entry = {"local": "p", "email": user, "dept": "Finance", "project": "P", "plan": plan, "pool": pool, "msg_bytes": 3000,
             "share": {"colleague": ["real-colleague-file"], "shared_drive": ["real-drive-file"], "outside": 0}}
    seed.seed_one_user(seed.Settings(), entry, [user], OUTSIDE, "tiny", 10, 5, False, only=frozenset({"drive", "gmail"}))
    text = b"".join(base64.urlsafe_b64decode(m["raw"]) for m in world.gmails[user].messages.values())
    assert text.count(b"real-colleague-file") == 10 and text.count(b"real-drive-file") == 10

def test_a_kind_with_no_real_item_falls_back_to_the_pool_and_outside_always_does():
    import random
    rng = random.Random(1)
    items = {"pool": ["p1", "p2"], "colleague": ["c"], "shared_drive": []}
    assert profile_seed._link_id(items, "colleague", rng) == "c"
    assert profile_seed._link_id(items, "shared_drive", rng) in ("p1", "p2")
    assert profile_seed._link_id(items, "outside", rng) in ("p1", "p2")
