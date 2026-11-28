"""fableplan2 P4.2-P4.5: a synthetic organisation drawn from a real one's shape, priced before it is written, and
safe to start again after a kill. Runs against the in-memory Drive and Gmail; no tenant, no network."""
import copy
import os
import re

import pytest

import profile_plan as pp
import profile_seed
import seed_sandbox as seed
from profile_extract import _pct
from tests.fakes import FakeCalendar, FakeChat, FakeDrive, FakeGmail, FakePeople, FakeProcessKilled, FakeTasks

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "profiles", "org45.json")


@pytest.fixture(scope="module")
def profile():
    return pp.load(PROFILE)


@pytest.fixture(scope="module")
def plans300(profile):
    return pp.plan(profile, 300, 1)


# ---- P4.5: the plan -------------------------------------------------------------------------------------------
def test_same_seed_same_plan_and_another_seed_another(profile):
    assert pp.plan(profile, 60, 7) == pp.plan(profile, 60, 7)
    assert pp.plan(profile, 60, 7) != pp.plan(profile, 60, 8)
    a, b = list(pp.tree(pp.plan(profile, 5, 7)[2])), list(pp.tree(pp.plan(profile, 5, 7)[2]))
    assert a == b


@pytest.mark.parametrize("n", [45, 100, 300])
def test_p50_and_p90_of_files_folders_and_messages_follow_the_profile(profile, n):
    plans = pp.plan(profile, n, 3)
    for ours, theirs in (("files", "file_count"), ("folders", "folder_count"), ("messages", "messages_total")):
        real, mine = [r[theirs] for r in profile["per_user"]], [p[ours] for p in plans]
        for q in (0.5, 0.9):
            assert abs(_pct(mine, q) - _pct(real, q)) <= 0.15 * _pct(real, q), (ours, q, n)


def test_the_tail_survives(profile, plans300):
    real_msgs = [r["messages_total"] for r in profile["per_user"]]
    assert max(p["messages"] for p in plans300) == max(real_msgs) > 40_000
    assert max(p["files"] for p in plans300) == max(r["file_count"] for r in profile["per_user"]) > 5_600
    assert sum(1 for p in plans300 if p["files"] <= 5) >= 3                      # several users with almost nothing
    assert sum(1 for p in plans300 if p["labels"] == 0) > len(plans300) / 2     # most have no label of their own
    assert max(p["labels"] for p in plans300) >= 20
    # a user's Drive numbers move together: the biggest Drive is also the widest
    big = max(plans300, key=lambda p: p["files"])
    assert big["folders"] >= max(p["folders"] for p in plans300) * 0.5


def test_profile_service_rates_are_explicit_assumptions_from_the_seed_defaults(plans300):
    assert {p["calendar_base_events"] for p in plans300} == {56}
    assert {p["calendar_events"] for p in plans300} == {64}
    assert {p["secondary_calendars"] for p in plans300} == {2}
    assert {p["contacts"] for p in plans300} == {25}
    assert {p["contact_groups"] for p in plans300} == {2}
    assert {p["tasks"] for p in plans300} == {20}
    assert {p["task_lists"] for p in plans300} == {1}
    import profile_plan
    source = open(profile_plan.__file__, encoding="utf-8").read()
    seed_source = open(seed.__file__, encoding="utf-8").read()
    corpus_source = open(__import__("corpus").__file__, encoding="utf-8").read()
    assert 'SCALES["medium"]["per_leaf"]' in source
    assert '"medium": {"per_leaf": 14' in corpus_source
    assert "seed_secondary_calendars" in seed_source
    assert "def seed_contacts" in seed_source and "def seed_tasks" in seed_source


def test_link_kind_split_is_a_validated_plan_input(profile):
    split = {"own": 0, "colleague": 0, "shared_drive": 0, "folder": 0, "deleted": 0, "outside": 5}
    plans = pp.plan(profile, 45, 8, link_kind_split=split)
    assert all(p["link_kind_split"] == {k: v / 5 for k, v in split.items()} for p in plans)
    assert all(p["link_kinds"]["outside"] == p["link_mail"] for p in plans)
    with pytest.raises(ValueError, match="exactly"):
        pp.plan(profile, 1, 8, link_kind_split={"own": 1})
    with pytest.raises(ValueError, match="non-negative"):
        pp.plan(profile, 1, 8, link_kind_split={**pp.DEFAULT_LINK_KIND_SPLIT, "own": -1})


def test_file_type_shares_follow_the_histogram_within_three_points(profile, plans300):
    mix = pp.file_mix(profile)
    want = {m: n / sum(mix.values()) for m, n in mix.items()}
    got: dict = {}
    for p in plans300:
        for m, n in p["mime"].items():
            got[m] = got.get(m, 0) + n
    total = sum(got.values())
    assert max(abs(got.get(m, 0) / total - want.get(m, 0)) for m in set(got) | set(want)) < 0.03
    assert abs(got["application/vnd.openxmlformats-officedocument.wordprocessingml.document"] / total - 0.31) < 0.03


def test_threads_labels_and_link_mail_follow_the_profile(profile, plans300):
    msgs, threads = sum(p["messages"] for p in plans300), sum(p["threads"] for p in plans300)
    real = sum(r["messages_total"] for r in profile["per_user"]) / sum(r["threads_total"] for r in profile["per_user"])
    assert abs(msgs / threads - real) / real < 0.1
    links = sum(p["link_mail"] for p in plans300)
    assert abs(links / msgs - profile["link_mail"]["messages"] / sum(r["messages_total"] for r in profile["per_user"])) < 0.005
    for p in plans300:
        assert sum(p["link_kinds"].values()) == p["link_mail"]
        assert p["threads"] <= p["messages"]
    assert {k for p in plans300 for k, n in p["link_kinds"].items() if n} == set(pp.LINK_KINDS)


@pytest.mark.parametrize("count", ["threads", "link_mail"])
def test_scaled_mail_preserves_thread_and_link_proportions(plans300, count):
    scaled = pp.scale_messages(copy.deepcopy(plans300), 0.15)
    assert scaled == pp.scale_messages(copy.deepcopy(plans300), 0.15)

    for original, reduced in zip(plans300, scaled):
        assert reduced["messages"] == int(original["messages"] * 0.15)
        assert (1 if reduced["messages"] else 0) <= reduced["threads"] <= reduced["messages"]
        assert 0 <= reduced["link_mail"] <= reduced["messages"]
        assert sum(reduced["link_kinds"].values()) == reduced["link_mail"]

    original_share = sum(p[count] for p in plans300) / sum(p["messages"] for p in plans300)
    reduced_share = sum(p[count] for p in scaled) / sum(p["messages"] for p in scaled)
    assert reduced_share == pytest.approx(original_share, rel=0.02)


@pytest.mark.parametrize("factor, retained", [(0, [0, 0, 0]), (0.5, [0, 0, 1])])
def test_scaled_mail_handles_empty_and_single_message_mailboxes(factor, retained):
    plans = [
        {"messages": 0, "threads": 0, "link_mail": 0},
        {"messages": 1, "threads": 1, "link_mail": 1},
        {"messages": 3, "threads": 1, "link_mail": 3},
    ]
    assert pp.scale_messages(plans, factor) is plans
    for plan, expected in zip(plans, retained):
        assert plan["messages"] == plan["threads"] == plan["link_mail"] == expected
        assert set(plan["link_kinds"]) == set(pp.LINK_KINDS)
        assert sum(plan["link_kinds"].values()) == expected


@pytest.mark.parametrize("factor", [1, 1.25])
def test_full_mail_share_leaves_plan_untouched(factor, plans300):
    plans = copy.deepcopy(plans300)
    assert pp.scale_messages(plans, factor) is plans
    assert plans == plans300


def test_a_tree_has_the_planned_counts_depth_names_and_dates(plans300):
    seen = {"deva": 0, "long": 0, "dup": 0, "breaker": 0, "old": 0}
    for p in plans300[:60]:
        nodes = list(pp.tree(p))
        assert sum(n["kind"] == "folder" for n in nodes) == p["folders"]
        assert sum(n["kind"] != "folder" for n in nodes) == p["files"]
        assert sum(n["kind"] == "shortcut" for n in nodes) == p["shortcuts"]
        depth = {-1: 0}
        for n in nodes:
            assert n["parent"] < n["i"] and n["modified_days"] <= n["created_days"]
            if n["kind"] == "folder":
                depth[n["i"]] = depth[n["parent"]] + 1
        assert max(depth.values()) == p["depth"]
        names = [(n["parent"], n["name"]) for n in nodes if n["kind"] == "file"]
        seen["dup"] += len(names) - len(set(names))
        for n in nodes:
            seen["deva"] += bool(re.search("[ऀ-ॿ]", n["name"]))
            seen["long"] += len(n["name"]) > 150
            seen["breaker"] += bool(re.search(r'[/\\:*?"<>|#%]|\.\.|^ | $|\.$', n["name"]))
            seen["old"] += n["created_days"] > 5 * 365
    assert all(seen.values()), seen


# ---- P4.3: the budget -----------------------------------------------------------------------------------------
def _plan_only(monkeypatch, capsys, *extra, users="300"):
    monkeypatch.setenv("SEED_CAMPAIGN", "0")
    rc = seed.main(["--confirm-domain", "tenanta.com", "--profile", PROFILE, "--users", users, "--seed", "1",
                    "--plan-only", "--workers", "8", *extra])
    return rc, capsys.readouterr().out


def test_the_budget_prints_what_it_costs_and_refuses_past_max_hours(monkeypatch, capsys):
    rc, out = _plan_only(monkeypatch, capsys, "--upload-mb-per-sec", "0.9", "--drive-per-sec", "20", "--mail-per-sec", "20",
                         "--max-hours", "1")
    assert rc == 1 and "REFUSED" in out and "--max-hours 1" in out
    for word in ("items", "Upload payloads across this box", "phase hours", "pool file", "profile maximum",
                 "Drive     :", "Gmail     :", "Calendar  :", "Contacts  :", "Tasks     :", "269 new profile users",
                 "Gmail settings: 1,495 writes + 598 existence reads (2,093 API calls) across 299 mailboxes",
                 "Directory : 269 new accounts, 299 account lookups"):
        assert word in out
    rc, out = _plan_only(monkeypatch, capsys, "--upload-mb-per-sec", "0.9", "--drive-per-sec", "20", "--mail-per-sec", "20",
                         "--max-hours", "100")
    assert rc == 0 and "REFUSED" not in out


def test_the_default_rates_come_from_the_repos_own_measurements(monkeypatch, capsys):
    import resources
    rc, out = _plan_only(monkeypatch, capsys, "--upload-mb-per-sec", "0.9", "--max-hours", "100000")
    assert rc == 0 and f"{resources.DRIVE_WRITES_PER_SEC:g} Drive writes/s" in out


def test_the_corrected_300_user_plan_command_runs_offline_and_reserves_every_seat(monkeypatch, capsys):
    monkeypatch.setenv("SEED_CAMPAIGN", "0")
    rc = seed.main(["--profile", PROFILE, "--users", "300", "--plan-only"])
    out = capsys.readouterr().out
    assert rc == 1 and "REFUSED" in out       # cost limit, not a licence overflow
    assert "Profile plan: 300 of 300 tenant seats" in out
    assert "admin 1 + existing cohort 30 (including twins) + 269 new profile users" in out
    assert "new accounts to create: 269" in out
    assert "23 Nov 2026 5-user profile-seed upload probe" in out


def test_plan_rejects_more_than_the_business_starter_seat_limit(monkeypatch, capsys):
    monkeypatch.setenv("SEED_CAMPAIGN", "0")
    with pytest.raises(SystemExit, match="exceeds --seat-limit"):
        seed.main(["--profile", PROFILE, "--users", "301", "--plan-only"])
    assert "Nothing was written" not in capsys.readouterr().out


def test_a_dry_plan_still_requires_a_users_number(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _plan_only(monkeypatch, capsys, users="a,b")
    with pytest.raises(SystemExit):
        _plan_only(monkeypatch, capsys, "--upload-mb-per-sec", "1", users="a,b")


def test_message_sizes_shrink_then_counts_scale_to_fit(plans300):
    kw = dict(drive_per_sec=1e6, mail_per_sec=1e6, workers=300, pool_bytes=0, pool_files=0, max_hours=12)
    roomy = pp.budget(plans300, upload_bps=1e9, **kw)
    assert roomy["msg_bytes"] == pp.NATURAL_MSG_BYTES and roomy["count_factor"] == 1
    tight = pp.budget(plans300, upload_bps=0.9e6, **kw)             # the box's line: 38 GB in 12 h
    assert pp.MIN_MSG_BYTES < tight["msg_bytes"] < pp.NATURAL_MSG_BYTES and tight["count_factor"] == 1
    assert tight["mail_bytes"] <= 0.9e6 * 12 * 3600 and not tight["refuse"]
    starved = pp.budget(plans300, upload_bps=1e4, **kw)             # not even 1 KB a message fits
    assert starved["msg_bytes"] == pp.MIN_MSG_BYTES and 0 < starved["count_factor"] < 1
    assert starved["messages"] == int(starved["messages_planned"] * starved["count_factor"])
    scaled = pp.scale_messages(copy.deepcopy(plans300), starved["count_factor"])
    assert sum(p["messages"] for p in scaled) <= starved["messages"] + len(scaled)
    assert not starved["refuse"] and starved["hours"] <= 12 + 1e-9


def test_account_creation_budget_counts_serial_lookups_and_inserts(plans300):
    result = pp.budget(plans300[:2], upload_bps=1e9, drive_per_sec=1e9,
                       mail_per_sec=1e9, per_user_per_sec=1e9, workers=8,
                       pool_bytes=0, pool_files=0, max_hours=100,
                       new_accounts=7, fixed_users=4, directory_per_sec=2)
    # Two profile users + three non-admin fixed users are looked up; seven
    # new users are inserted. Directory provisioning is serial despite the
    # eight-user seed worker pool.
    assert result["items_by_service"]["directory_user_lookups"] == 5
    assert result["items_by_service"]["directory_users_created"] == 7
    assert result["items_by_service"]["gmail_settings_users"] == 5
    assert result["items_by_service"]["gmail_settings_writes"] == 25
    assert result["items_by_service"]["gmail_settings_reads"] == 10
    assert result["phase_hours"]["account_creation"] == pytest.approx(26 / 3600)
    assert result["phase_hours"]["gmail_settings"] == pytest.approx(35 / (1e9 * 2) / 3600)


def test_the_pool_has_a_largest_file_below_the_profiles(profile):
    sizes = pp.pool_sizes(pp.file_mix(profile), 5_000_000_000, 50 * 2**20)
    assert max(max(v) for v in sizes.values()) == 50 * 2**20 < profile["summary"]["largest_bytes"]["max"]
    assert all(v == sorted(v) for v in sizes.values())
    small = pp.pool_sizes(pp.file_mix(profile), 50_000_000, 50 * 2**20)
    assert sum(sum(v) for v in small.values()) <= 50_000_000


def test_pool_can_budget_a_ten_gigabyte_tail(profile, plans300):
    # Decimal GB cap: --pool-max-file-mb accepts MiB and can be set up to the
    # profile's observed 10.1 GB maximum. A 40 GB budget can hold 10 GB.
    sizes = pp.pool_sizes({"application/pdf": 10}, 40_000_000_000, 10_000_000_000)
    actual_pool_bytes = sum(sum(v) for v in sizes.values())
    assert max(max(v) for v in sizes.values()) == 10_000_000_000
    budget = pp.budget(plans300[:1], upload_bps=1_000_000, drive_per_sec=1_000_000,
                       mail_per_sec=1_000_000, per_user_per_sec=1_000_000,
                       workers=1, pool_bytes=actual_pool_bytes, pool_files=sum(map(len, sizes.values())),
                       max_hours=100_000, new_accounts=0)
    assert budget["bytes_crossing_box"] >= actual_pool_bytes
    assert budget["phase_hours"]["pool_upload"] == pytest.approx(actual_pool_bytes / 1_000_000 / 3600)


# ---- P4.5: nothing that identifies a person -------------------------------------------------------------------
def test_the_profile_holds_numbers_and_file_types_only(profile, monkeypatch, capsys):
    mime = re.compile(r"^[a-z]+/[a-z0-9.+-]+$")

    def walk(x, key=""):
        if isinstance(x, dict):
            for k, v in x.items():
                assert key in ("mime_totals", "mime") and mime.match(k) or re.match(r"^[a-z_0-9]+$", k), k
                walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        else:
            assert isinstance(x, (int, float)), (key, x)

    walk(profile)
    text = open(PROFILE, encoding="utf-8").read()
    assert not re.search(r"@|https?://|\.com|\.gov|\.np", text)
    _, out = _plan_only(monkeypatch, capsys, "--upload-mb-per-sec", "0.9", "--max-hours", "1000000")
    assert "@" not in out and not re.search(r"[ऀ-ॿ]", out)


# ---- P4.4: a seed started again adds nothing twice ------------------------------------------------------------
class _NoLimiter:
    def acquire(self, n=1):
        pass

    def penalise(self):
        return 0.0


class _Media:
    def __init__(self, data, mimetype):
        self.data, self.mimetype = data, mimetype

    def read_all(self):
        return self.data


@pytest.fixture
def tenant(monkeypatch, settings):
    """One user's profile services in memory, with the seeder's pacing stubbed out."""
    drive, gmail = FakeDrive("p001@tenanta.com", "a"), FakeGmail("p001@tenanta.com", "a")
    cal = FakeCalendar("p001@tenanta.com", "a")
    people, tasks = FakePeople("p001@tenanta.com", "a"), FakeTasks("p001@tenanta.com", "a")
    FakeChat.reset_shared()
    chat = FakeChat("p001@tenanta.com", "a")
    monkeypatch.setattr(seed, "_media", _Media)
    monkeypatch.setattr(seed, "_retry_factory", lambda settings, limiter=None: (lambda fn: fn))
    monkeypatch.setattr(seed, "_seed_drive_limiter", lambda settings: _NoLimiter())
    monkeypatch.setattr(seed, "build_services", lambda settings, user: (drive, gmail, cal))
    monkeypatch.setattr(seed, "build_people_tasks", lambda settings, user: (people, tasks))
    monkeypatch.setattr(seed, "build_chat", lambda settings, user: chat)
    monkeypatch.setattr(seed, "build_gmail_settings", lambda settings, user: gmail)
    return drive, gmail, cal, people, tasks, chat


def _mini_profile(profile):
    """The real shape at a thirtieth of the size, so a fake can hold it."""
    mini = copy.deepcopy(profile)
    for r in mini["per_user"]:
        r["file_count"], r["folder_count"] = max(4, r["file_count"] // 40), r["folder_count"] // 12
        r["messages_total"], r["threads_total"] = max(2, r["messages_total"] // 25), max(2, r["threads_total"] // 25)
        r["shortcut_count"] = min(r["shortcut_count"], 2)
    mini["link_mail"]["messages"] = sum(r["messages_total"] for r in mini["per_user"]) * 4 // 100
    return mini


def _one(profile, settings, tenant):
    """A planned user (the one with most files of three) and its pool, ready for seed_one_user."""
    plan = max(pp.plan(_mini_profile(profile), 3, 5), key=lambda p: p["files"])
    outside_only = {"own": 0, "colleague": 0, "shared_drive": 0, "folder": 0, "deleted": 0, "outside": 1}
    plan.update(messages=230, threads=190, link_mail=12, link_kind_split=outside_only,
                link_kinds=pp._split(12, outside_only), labels=2)
    pool = profile_seed.build_pool(seed, settings, "p001@tenanta.com", pp.file_mix(profile), 2_000_000, 40_000, share_domain=True)
    # The real pool is owned by SOURCE_ADMIN, outside the generated profile
    # user list. Model that ownership on the fake pool items as well.
    pool["owner"] = "admin@tenanta.com"
    for fid in pool["ids"]:
        tenant[0].store[fid]["owners"] = [{"emailAddress": pool["owner"]}]
    return {"local": "p001", "email": "p001@tenanta.com", "dept": "Engineering", "project": "PRJ", "plan": plan, "pool": pool,
            "msg_bytes": 3000}


def _run(entry):
    users = [entry["email"], "peer@tenanta.com", "other@tenanta.com"]
    return seed.seed_one_user(seed.Settings(), entry, users, "ext@example.com", "tiny", 10, 5, False,
                              only=frozenset({"drive", "gmail", "calendar", "chat", "contacts", "tasks",
                                              "gmail_settings"}))


def _mine(drive):
    return [f for f in drive.store.values() if (f.get("appProperties") or {}).get("bpseed") == "1"]


def _ids(gmail):
    return [re.search(rb"Message-ID: <([^>]+)>", __import__("base64").urlsafe_b64decode(m["raw"])).group(1)
            for m in gmail.messages.values()]


def test_a_profile_user_lands_as_planned_through_copies_and_old_dates(profile, settings, tenant):
    drive, gmail, cal, people, tasks, chat = tenant
    entry = _one(profile, settings, tenant)
    plan, uploads = entry["plan"], drive.call_count("files.create")
    r = _run(entry)
    mine = _mine(drive)
    assert len([f for f in mine if f["appProperties"]["bpi"].isdigit()]) == plan["files"] + plan["folders"]
    assert r["drive"]["total_files"] == plan["files"]
    # every file is a copy of a pool file (plus the one made to be deleted, for a link to a deleted file)
    assert drive.call_count("files.copy") == plan["files"] - plan["shortcuts"] + bool(plan["link_kinds"]["deleted"])
    assert drive.call_count("files.create") - uploads == plan["folders"] + plan["shortcuts"] + 1    # + the root
    assert min(f["createdTime"] for f in mine if f.get("createdTime")) < "2022"      # created over years, set at creation
    assert r["gmail"]["messages"] == plan["messages"] == len(gmail.messages)
    assert len(set(_ids(gmail))) == plan["messages"]
    old = [m for m in gmail.messages.values() if b"Date: " in __import__("base64").urlsafe_b64decode(m["raw"])]
    assert len(old) == plan["messages"]
    assert any(b"In-Reply-To" in __import__("base64").urlsafe_b64decode(m["raw"]) for m in gmail.messages.values())
    assert r["gmail"]["with_drive_link"] == plan["link_mail"]
    assert len(gmail.labels) - len(FakeGmail("x", "y").labels) == plan["labels"]
    assert r["calendar"]["events"] == plan["calendar_base_events"] + 2
    assert r["calendar"]["calendars"] == plan["secondary_calendars"]
    assert r["calendar"]["calendar_events"] == plan["secondary_calendars"] * plan["secondary_events_per_calendar"]
    assert len(cal.store) + sum(len(x) for x in cal.cal_events.values()) == plan["calendar_events"]
    assert len(people.contacts) == plan["contacts"]
    assert len(people.groups) == plan["contact_groups"]
    assert sum(len(x) for x in tasks.task_store.values()) == plan["tasks"]
    assert len(tasks.lists) == plan["task_lists"]
    assert len(chat.space_store) == 2
    assert sum(len(x) for x in chat.message_store.values()) == 12
    assert len(gmail.forwarding_addresses) == len(gmail.mailbox_delegates) == 1
    outside = profile_seed.link_items(seed, drive, plan, entry["pool"], lambda fn: fn,
                                      user=entry["email"])
    assert outside["outside"]
    assert all(drive.store[fid]["owners"][0]["emailAddress"] == "admin@tenanta.com" for fid in outside["outside"])


def test_the_second_run_adds_nothing(profile, settings, tenant):
    drive, gmail, cal, people, tasks, chat = tenant
    entry = _one(profile, settings, tenant)
    _run(entry)
    state = (len(drive.store), len(gmail.messages), len(gmail.labels), len(cal.store), len(cal.calendar_store),
             len(people.contacts), len(people.groups), sum(map(len, people.group_members.values())),
             len(tasks.lists), sum(len(x) for x in tasks.task_store.values()),
             len(chat.space_store), sum(len(x) for x in chat.message_store.values()),
             len(gmail.forwarding_addresses), len(gmail.mailbox_delegates),
             gmail.vacation_settings.copy(), gmail.pop_settings.copy(), gmail.imap_settings.copy())
    drive.reset_calls()
    gmail.reset_calls()
    cal.reset_calls()
    people.reset_calls()
    tasks.reset_calls()
    chat.reset_calls()
    r = _run(entry)
    after = (len(drive.store), len(gmail.messages), len(gmail.labels), len(cal.store), len(cal.calendar_store),
             len(people.contacts), len(people.groups), sum(map(len, people.group_members.values())),
             len(tasks.lists), sum(len(x) for x in tasks.task_store.values()),
             len(chat.space_store), sum(len(x) for x in chat.message_store.values()),
             len(gmail.forwarding_addresses), len(gmail.mailbox_delegates),
             gmail.vacation_settings.copy(), gmail.pop_settings.copy(), gmail.imap_settings.copy())
    assert after == state
    calls = drive.calls + gmail.calls + cal.calls + people.calls + tasks.calls + chat.calls
    assert not [name for name, _ in calls if name in {
        "files.create", "files.copy", "messages.insert", "labels.create", "events.import",
        "calendars.insert", "acl.insert", "people.createContact", "contactGroups.create",
        "contactGroups.members.modify", "tasklists.insert", "tasks.insert", "spaces.create",
        "chat.messages.create", "forwardingAddresses.create", "delegates.create"}]
    assert r["gmail"].get("messages", 0) == 0 and r["drive"].get("total_files", 0) == 0


def test_a_seed_killed_in_drive_resumes_without_a_duplicate(profile, settings, tenant):
    drive, gmail, cal, people, tasks, chat = tenant
    entry = _one(profile, settings, tenant)
    drive.kill_after("files.copy", 8)
    with pytest.raises(FakeProcessKilled):
        _run(entry)
    partial = len(_mine(drive))
    assert 0 < partial < entry["plan"]["files"] + entry["plan"]["folders"] + 1
    _run(entry)
    nodes = [f["appProperties"]["bpi"] for f in _mine(drive) if f["appProperties"]["bpi"].isdigit()]
    assert len(nodes) == len(set(nodes)) == entry["plan"]["files"] + entry["plan"]["folders"]
    assert len(gmail.messages) == entry["plan"]["messages"]


def test_a_seed_killed_in_mail_resumes_without_a_duplicate(profile, settings, tenant):
    drive, gmail, cal, people, tasks, chat = tenant
    entry = _one(profile, settings, tenant)
    gmail.kill_after("messages.insert", 35)
    with pytest.raises(FakeProcessKilled):
        _run(entry)
    assert 0 < len(gmail.messages) < entry["plan"]["messages"]
    assert "gmail" not in profile_seed.done_services(drive, lambda fn: fn)
    r = _run(entry)
    ids = _ids(gmail)
    assert len(ids) == len(set(ids)) == entry["plan"]["messages"] and r["gmail"]["failed"] == 0
    assert "gmail" in profile_seed.done_services(drive, lambda fn: fn)


@pytest.mark.parametrize("service,method,successful_calls", [
    ("calendar", "events.import", 8),
    ("contacts", "people.createContact", 5),
    ("tasks", "tasks.insert", 5),
    ("chat", "chat.messages.create", 4),
    # This fires after the forwarding address committed and before delegate
    # creation, so replay must discover the existing address and add one delegate.
    ("gmail_settings", "delegates.list", 0),
])
def test_a_seed_interrupted_in_every_remaining_service_resumes_without_duplicates(
        profile, settings, tenant, service, method, successful_calls):
    drive, gmail, cal, people, tasks, chat = tenant
    entry = _one(profile, settings, tenant)
    api = {"calendar": cal, "contacts": people, "tasks": tasks,
           "chat": chat, "gmail_settings": gmail}[service]
    api.kill_after(method, successful_calls)
    with pytest.raises(FakeProcessKilled):
        _run(entry)
    done = profile_seed.done_services(drive, lambda fn: fn)
    if service != "gmail_settings":
        assert service not in done
    if service == "gmail_settings":
        assert len(gmail.forwarding_addresses) == 1 and not gmail.mailbox_delegates
    _run(entry)
    if service != "gmail_settings":
        assert service in profile_seed.done_services(drive, lambda fn: fn)
    assert len(drive.store)  # the user's root is the durable service journal
    if service == "calendar":
        uids = [e.get("iCalUID") for e in cal.store.values()]
        uids += [e.get("iCalUID") for events in cal.cal_events.values() for e in events.values()]
        assert len(uids) == len(set(uids)) == entry["plan"]["calendar_events"]
    elif service == "contacts":
        markers = [u.get("value") for c in people.contacts.values() for u in c.get("userDefined", [])
                   if u.get("key") == "seed_sandbox_index"]
        assert len(markers) == len(set(markers)) == entry["plan"]["contacts"]
        assert len(people.groups) == entry["plan"]["contact_groups"]
    elif service == "tasks":
        profile_tasks = [task for rows in tasks.task_store.values() for task in rows
                         if "[seed_sandbox_profile_task:" in task.get("notes", "")]
        assert len(profile_tasks) == entry["plan"]["tasks"]
        assert len({t["notes"] for t in profile_tasks}) == entry["plan"]["tasks"]
        assert len(tasks.lists) == entry["plan"]["task_lists"]
    elif service == "chat":
        assert len(chat.space_store) == 2
        assert sum(len(x) for x in chat.message_store.values()) == 12
    else:
        assert len(gmail.forwarding_addresses) == len(gmail.mailbox_delegates) == 1


def test_the_upload_probe_measures_and_cleans_up(settings, tenant):
    drive, _, _, _, _, _ = tenant
    rate = profile_seed.probe_upload(seed, settings, "p001@tenanta.com", 0.0, chunk=100_000)
    assert rate > 0 and not [f for f in drive.store.values() if f["name"].startswith("bitport-upload-probe")]


def test_mail_share_cuts_every_mailbox_before_the_cost_is_worked_out(monkeypatch, capsys):
    """Operator, 23 Nov 2026: test 15% of the profile's mail -- the whole of it was 200 h through the box's upload."""
    import re
    args = ("--upload-mb-per-sec", "0.9", "--drive-per-sec", "20", "--mail-per-sec", "20", "--max-hours", "100000")
    _, full = _plan_only(monkeypatch, capsys, *args)
    _, cut = _plan_only(monkeypatch, capsys, *args, "--mail-share", "0.15")
    msgs = lambda out: int(re.search(r"([\d,]+) messages", out).group(1).replace(",", ""))
    assert 0.13 * msgs(full) <= msgs(cut) <= 0.15 * msgs(full)
