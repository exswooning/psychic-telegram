"""fableplan2 P4.2/P4.3: what a synthetic organisation holds, drawn from a real one's shape, and what it costs.

`plan()` turns profiles/org45.json (numbers only) into one target per user: files, folders, depth, a file-type
mix, bytes, messages, threads, labels, Drive-link mail. Each number is read off the profile's sorted per-user
vector at the user's RANK, so the tail survives at any size: with 300 users one mailbox is at the profile's maximum,
several are at its minimum, and p50/p90 match. A user's Drive numbers move together (the biggest Drive is also the
widest and deepest), so do its mail numbers; Drive and mail are drawn independently, and so are labels. Nothing
here touches Google or the clock: the same (profile, users, seed) is the same plan in every process.

`tree()` expands one user's target into folders and files (names, times, sizes). `budget()` says what seeding the
plan costs from measured rates, and shrinks message sizes, then counts, until the mail fits the window.
"""
from __future__ import annotations

import json
import random
import zlib

GOOGLE_APPS = "application/vnd.google-apps."
FOLDER = GOOGLE_APPS + "folder"
SHORTCUT = GOOGLE_APPS + "shortcut"
NATIVES = (GOOGLE_APPS + "document", GOOGLE_APPS + "spreadsheet", GOOGLE_APPS + "presentation")

# The profile has no figure for these; these are assumptions, not extracted
# observations.  The named default is printed with each plan and can be
# replaced by --link-kind-split.
DEFAULT_LINK_KIND_SPLIT = {"own": 0.40, "colleague": 0.25, "shared_drive": 0.10,
                          "folder": 0.10, "deleted": 0.05, "outside": 0.10}
LINK_KINDS = DEFAULT_LINK_KIND_SPLIT       # compatibility for existing callers
OUTSIDE_ADDRESS = "learn.aryanpaul@gmail.com"   # P0.6 (17 Nov, the operator): the only address outside the tenant ever shared with
# Departments and their relative size (the profile says nothing about either), and the rest of the sharing shape: the
# plan's own numbers, stated here and nowhere else.
DEPARTMENTS = {"Administration": 5, "Finance": 3, "Planning": 3, "Health": 4, "Education": 4, "Engineering": 3, "Social": 2,
               "Records": 1}
SHARING = {"readers": 5,             # people who read the shared mailbox
           "users_per_drive": 25,    # one shared drive for this many users
           "drive_members": 0.12,    # share of the users on each shared drive
           "outside_users": 0.15,    # share of the users who share something with the outside address
           "outside_files": (1, 3)}  # how many of their files
DUPLICATE_NAME = 0.02      # a file named exactly like the one before it, in the same folder
PATH_BREAKING = 0.015       # a name with a character that breaks a path
LONG_NAME = 0.03
DEVANAGARI = 0.25
MIN_MSG_BYTES = 1024        # a header block and a one-line body: the smallest message the generator makes
NATURAL_MSG_BYTES = 1_700_000   # 245 GB over 141,416 messages (the plan's table; the profile holds no mail bytes)
RECORDED_PLAN_UPLOAD_MB_PER_SEC = 1.84  # 23 Nov 2026, 5-user profile seed's 60s box upload probe; assumption for offline --plan-only
DEFAULT_DIRECTORY_REQUESTS_PER_SEC = 1.0  # explicit conservative assumption: sequential Directory get/insert requests

# account 3's ledger has no useful calendar/contact/task rows (P4.1). These
# explicit assumptions follow existing seed_sandbox.py defaults: medium
# calendar = SCALES["medium"]["per_leaf"] * 4 events (the value 14 is defined
# in corpus.py), plus its weekly and all-day examples;
# two secondary calendars with three events each; 25 contacts and 20 tasks.
# Profile mode does not silently inherit --scale, so the assumed rates live
# here and appear in every per-user plan.
PROFILE_CALENDAR_BASE_EVENTS = 56
PROFILE_CALENDAR_FIXED_EVENTS = 2
PROFILE_SECONDARY_CALENDARS = 2
PROFILE_EVENTS_PER_SECONDARY_CALENDAR = 3
PROFILE_CONTACTS_PER_USER = 25
PROFILE_CONTACT_GROUPS_PER_USER = 2
PROFILE_TASKS_PER_USER = 20
PROFILE_TASK_LISTS_PER_USER = 1
# Each mailbox seeded by seed_gmail_settings() gets three settings updates,
# one forwarding address and one delegate when peers are present. Profile mode
# always runs beside its cohort, so every seeded mailbox has peers; settings
# are applied to cohort mailboxes too even when their fixture content is kept.
PROFILE_GMAIL_SETTINGS_WRITES_PER_USER = {
    "vacation": 1,
    "pop": 1,
    "imap": 1,
    "forwarding_address": 1,
    "personal_delegate": 1,
}
PROFILE_GMAIL_SETTINGS_READS_PER_USER = {"forwarding_addresses": 1, "delegates": 1}

_LATIN = ["Report", "Budget", "Minutes", "Invoice", "Survey", "Plan", "Notes", "Photo", "Scan", "Draft", "Tender",
          "Ward", "Census", "Permit", "Letter", "Proposal", "Audit", "Contract", "Schedule", "Summary", "Map"]
_DEVA = ["प्रतिवेदन", "बजेट", "निर्णय", "सूचना", "फोटो", "पत्र", "बैठक", "विवरण", "योजना", "नागरिकता", "कार्यालय", "सभा"]
_BREAKERS = ["{0}/{0}", "{0}\\{0}", "{0}:{0}", "{0}*", "{0}?", '"{0}"', "<{0}>", "{0}|{0}", "#{0}", "{0}%", "{0}..", " {0}", "{0} ", "{0}."]
_EXT = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/bmp": "bmp", "image/heif": "heic",
        "application/msword": "doc", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        "application/vnd.ms-excel": "xls", "audio/mpeg": "mp3", "application/x-font-ttf": "ttf",
        "video/mp4": "mp4", "text/plain": "txt", "application/zip": "zip"}


def _rng(seed: int, *salt) -> random.Random:
    return random.Random(zlib.crc32("\x1f".join(str(x) for x in (seed, *salt)).encode("utf-8")))


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _by_rank(values: list, n: int) -> list[int]:
    """n values read off the sorted vector at ranks 0..1: the first is its minimum, the last its maximum. Nearest rank,
    not a straight line between neighbours: the profile's own p90 (a vector of 45) sits on a cliff, and a straight
    line put the plan's p90 15% off at some sizes. Every number is one the real organisation had."""
    s, m = sorted(values), len(values)
    if n == 1:
        return [s[m // 2]]
    return [s[round(i * (m - 1) / (n - 1))] for i in range(n)]


def file_mix(profile: dict) -> dict:
    """The profile's histogram without folders and shortcuts (those are structure), Forms and scripts counted as Docs."""
    mix: dict = {}
    for mime, n in profile["mime_totals"].items():
        if mime in (FOLDER, SHORTCUT):
            continue
        if mime.startswith(GOOGLE_APPS) and mime not in NATIVES:
            mime = NATIVES[0]
        mix[mime] = mix.get(mime, 0) + n
    return mix


def validate_link_kind_split(split: dict | None = None) -> dict:
    """Normalize a link-kind distribution after validating its keys and weights."""
    split = DEFAULT_LINK_KIND_SPLIT if split is None else split
    if set(split) != set(DEFAULT_LINK_KIND_SPLIT):
        missing = sorted(set(DEFAULT_LINK_KIND_SPLIT) - set(split))
        extra = sorted(set(split) - set(DEFAULT_LINK_KIND_SPLIT))
        raise ValueError(f"link-kind split needs exactly {sorted(DEFAULT_LINK_KIND_SPLIT)} (missing={missing}, extra={extra})")
    values = {k: float(v) for k, v in split.items()}
    if any(v < 0 for v in values.values()) or sum(values.values()) <= 0:
        raise ValueError("link-kind weights must be non-negative and have a positive total")
    total = sum(values.values())
    return {k: v / total for k, v in values.items()}


def plan(profile: dict, users: int, seed: int, link_kind_split: dict | None = None) -> list[dict]:
    link_kind_split = validate_link_kind_split(link_kind_split)
    rows = profile["per_user"]
    vec = lambda k: [r[k] for r in rows]                      # noqa: E731
    msg_total = sum(vec("messages_total")) or 1
    link_ratio = profile["link_mail"]["messages"] / msg_total
    mix = file_mix(profile)
    mimes, weights = list(mix), list(mix.values())
    orders = {g: list(range(users)) for g in ("drive", "mail", "labels")}
    for g, order in orders.items():
        _rng(seed, "order", g).shuffle(order)

    d = {k: _by_rank(vec(k), users) for k in ("file_count", "folder_count", "max_depth", "shortcut_count", "total_bytes",
                                                "largest_bytes")}
    m = {k: _by_rank(vec(k), users) for k in ("messages_total", "threads_total")}
    lab = _by_rank(vec("user_label_count"), users)
    out = []
    for i in range(users):
        rd, rm, rl = orders["drive"][i], orders["mail"][i], orders["labels"][i]
        rng = _rng(seed, "user", i)
        files = d["file_count"][rd]
        folders = d["folder_count"][rd]
        shortcuts = min(d["shortcut_count"][rd], max(0, files - 1))
        messages = m["messages_total"][rm]
        links = min(messages, round(messages * link_ratio))
        kinds = _split(links, link_kind_split)
        counts: dict = {}
        for mime in rng.choices(mimes, weights=weights, k=files - shortcuts):
            counts[mime] = counts.get(mime, 0) + 1
        out.append({
            "index": i, "key": rng.getrandbits(32),
            "files": files, "folders": folders, "depth": min(d["max_depth"][rd], folders),
            "shortcuts": shortcuts, "mime": counts,
            "bytes": d["total_bytes"][rd], "largest": min(d["largest_bytes"][rd], d["total_bytes"][rd]),
            "messages": messages, "threads": max(1, min(m["threads_total"][rm], messages)) if messages else 0,
            "labels": lab[rl], "link_mail": links, "link_kinds": kinds,
            "link_kind_split": dict(link_kind_split),
            "calendar_base_events": PROFILE_CALENDAR_BASE_EVENTS,
            "secondary_calendars": PROFILE_SECONDARY_CALENDARS,
            "secondary_events_per_calendar": PROFILE_EVENTS_PER_SECONDARY_CALENDAR,
            "calendar_events": (PROFILE_CALENDAR_BASE_EVENTS + PROFILE_CALENDAR_FIXED_EVENTS
                                + PROFILE_SECONDARY_CALENDARS * PROFILE_EVENTS_PER_SECONDARY_CALENDAR),
            "contacts": PROFILE_CONTACTS_PER_USER,
            "contact_groups": PROFILE_CONTACT_GROUPS_PER_USER,
            "tasks": PROFILE_TASKS_PER_USER,
            "task_lists": PROFILE_TASK_LISTS_PER_USER,
        })
    return out


def sharing(plans: list[dict], seed: int) -> dict:
    """Who belongs where, by user index: a department each (its lead is the first member), the shared mailbox (the biggest
    mailbox) and its readers, the members of each shared drive, and how many files each user shares outside. Only
    (plans, seed) decide it."""
    n, names = len(plans), list(DEPARTMENTS)
    dept = [_rng(seed, "dept", i).choices(names, weights=list(DEPARTMENTS.values()))[0] for i in range(n)]
    mailbox = max(range(n), key=lambda i: (plans[i]["messages"], -i)) if n else None
    others = [i for i in range(n) if i != mailbox]
    readers = sorted(_rng(seed, "mailbox").sample(others, min(SHARING["readers"], len(others))))
    drives = [sorted(_rng(seed, "sd", d).sample(range(n), min(n, max(2, round(n * SHARING["drive_members"])))))
              for d in range(round(n / SHARING["users_per_drive"]) if n > 1 else 0)]
    outside = {}
    for i, p in enumerate(plans):
        r = _rng(seed, "outside", i)
        if p["files"] > p["shortcuts"] and r.random() < SHARING["outside_users"]:
            outside[i] = min(r.randint(*SHARING["outside_files"]), p["files"] - p["shortcuts"])
    return {"dept": dept, "lead": {d: dept.index(d) for d in sorted(set(dept))}, "mailbox": mailbox, "readers": readers,
            "drives": drives, "outside": outside}


def _split(total: int, shares: dict) -> dict:
    """total divided by shares in whole numbers that add up to total (largest remainders)."""
    raw = {k: total * v for k, v in shares.items()}
    out = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[: total - sum(out.values())]:
        out[k] += 1
    return out


def scale_messages(plans: list[dict], factor: float) -> list[dict]:
    """Shrink each mailbox while preserving its thread and link-mail proportions."""
    if factor >= 1:
        return plans
    for p in plans:
        original = p["messages"]
        retained = int(original * factor)
        share = retained / original if original else 0
        p["messages"] = retained
        p["threads"] = max(1, round(p["threads"] * share)) if retained else 0
        p["link_mail"] = round(p["link_mail"] * share)
        p["link_kinds"] = _split(p["link_mail"], p.get("link_kind_split", DEFAULT_LINK_KIND_SPLIT))
    return plans


# ----------------------------------------------------------------------------------------------------------------
# One user's tree
# ----------------------------------------------------------------------------------------------------------------
def name(rng: random.Random, ext: str = "") -> str:
    """A file or folder name: Latin or Devanagari words, sometimes very long, sometimes with a path-breaking character."""
    words = _DEVA if rng.random() < DEVANAGARI else _LATIN
    base = f"{rng.choice(words)} {rng.choice(words)} {rng.randint(1, 2026)}"
    if rng.random() < LONG_NAME:
        base = " ".join([base] + [rng.choice(words) for _ in range(rng.randint(25, 40))])
    if rng.random() < PATH_BREAKING:
        base = rng.choice(_BREAKERS).format(base)
    return base + (f".{ext}" if ext else "")


def _days(rng: random.Random) -> tuple[int, int]:
    """(created, modified) in days ago, spread over years; modified is never before created."""
    created = int(rng.random() ** 1.2 * 3650) + 30
    return created, int(created * rng.random() ** 0.5)


def tree(p: dict):
    """One user's folders, files and shortcuts, parents before children. Node ids are positions: folders 0..F-1,
    then files, then shortcuts; parent -1 is the user's root. The same plan entry is the same tree."""
    rng = _rng(p["key"], "tree")
    folders, depth_of = p["folders"], []
    for i in range(folders):
        parent = i - 1 if i < p["depth"] else -1
        if i >= p["depth"] and i:
            for _ in range(4):                               # skewed to early folders: a few wide ones
                j = int(rng.random() ** 2 * i)
                if depth_of[j] + 1 < max(p["depth"], 1):
                    parent = j
                    break
        depth_of.append(0 if parent < 0 else depth_of[parent] + 1)
        c, mod = _days(rng)
        yield {"i": i, "kind": "folder", "parent": parent, "name": name(rng), "mime": FOLDER, "size": 0,
               "created_days": c, "modified_days": mod}

    mimes = [m for m, n in sorted(p["mime"].items()) for _ in range(n)]
    rng.shuffle(mimes)
    binaries = [m for m in mimes if m not in NATIVES]
    weights = [rng.paretovariate(1.3) for _ in binaries]
    spare = max(0, p["bytes"] - p["largest"])
    sizes = iter([max(1024, int(spare * w / sum(weights))) for w in weights])
    big = rng.randrange(len(binaries)) if binaries else -1
    n_bin, prev = 0, None
    for k, mime in enumerate(mimes):
        i = folders + k
        parent = -1 if not folders or rng.random() < 0.15 else int(rng.random() ** 1.5 * folders)
        nm = name(rng, _EXT.get(mime, "bin") if mime not in NATIVES else "")
        if prev and prev["mime"] == mime and rng.random() < DUPLICATE_NAME:
            nm, parent = prev["name"], prev["parent"]
        size = 0
        if mime not in NATIVES:
            size = p["largest"] if n_bin == big else next(sizes)
            n_bin += 1
        c, mod = _days(rng)
        prev = {"i": i, "kind": "file", "parent": parent, "name": nm, "mime": mime, "size": size,
                "created_days": c, "modified_days": mod}
        yield prev
    for s in range(p["shortcuts"] if mimes else 0):
        target = folders + rng.randrange(len(mimes))
        c, mod = _days(rng)
        yield {"i": folders + len(mimes) + s, "kind": "shortcut", "parent": -1, "name": "Shortcut to " + name(rng),
               "mime": SHORTCUT, "size": 0, "target": target, "created_days": c, "modified_days": mod}


# ----------------------------------------------------------------------------------------------------------------
# What it costs
# ----------------------------------------------------------------------------------------------------------------
def pool_sizes(mix: dict, pool_bytes: int, largest_cap: int) -> dict:
    """The sizes of the unique files uploaded once: mime -> ascending sizes. A common file type (above the mean share)
    gets four, each an eighth of the one above; a rare one gets one small file. The largest is what the pool
    can afford, never above largest_cap."""
    binary = {m: n for m, n in mix.items() if m not in NATIVES}
    mean = sum(binary.values()) / max(1, len(binary))
    big = [m for m, n in binary.items() if n >= mean]
    rare = [m for m in binary if m not in big]
    unit = len(big) * (1 + 1 / 8 + 1 / 64 + 1 / 512) + len(rare) / 512
    largest = max(MIN_MSG_BYTES, min(int(pool_bytes / unit) if unit else largest_cap, largest_cap))
    sizes = {m: sorted({max(MIN_MSG_BYTES, largest // 8 ** j) for j in range(4)}) for m in big}
    sizes.update({m: [max(MIN_MSG_BYTES, largest // 512)] for m in rare})
    return sizes


def service_counts(plans: list[dict], seed: int, pool_files: int = 0,
                   settings_users: int | None = None) -> dict:
    """Logical item counts for every profile service, including the tenant sharing layer."""
    users = len(plans)
    settings_users = users if settings_users is None else settings_users
    sh = sharing(plans, seed)
    active_depts = sum(1 for d in set(sh["dept"]) if sh["dept"].count(d) > 1)
    dept_files = active_depts * 4
    dept_folders = active_depts
    sd_count = len(sh["drives"])
    sd_members = sum(len(group) for group in sh["drives"])
    sd_files, sd_folders = sd_count * 16, sd_count * 2
    outside_grants = sum(sh["outside"].values())
    dept_groups = len(set(sh["dept"]))
    dept_members = users
    return {
        "users": users,
        "drive_files": sum(p["files"] for p in plans) + pool_files + dept_files + sd_files,
        "drive_folders": sum(p["folders"] for p in plans) + users + 1 + dept_folders + sd_folders,
        "drive_copies": sum(p["files"] - p["shortcuts"] for p in plans) + dept_files + sd_files,
        "drive_shortcuts": sum(p["shortcuts"] for p in plans),
        "drive_permissions": outside_grants + sd_members + dept_folders + bool(pool_files),
        "shared_drives": sd_count,
        "shared_drive_members": sd_members,
        "gmail_messages": sum(p["messages"] for p in plans),
        "gmail_labels": sum(p["labels"] for p in plans),
        "gmail_delegates": len(sh["readers"]),
        "gmail_settings_users": settings_users,
        "gmail_vacation_settings": settings_users * PROFILE_GMAIL_SETTINGS_WRITES_PER_USER["vacation"],
        "gmail_pop_settings": settings_users * PROFILE_GMAIL_SETTINGS_WRITES_PER_USER["pop"],
        "gmail_imap_settings": settings_users * PROFILE_GMAIL_SETTINGS_WRITES_PER_USER["imap"],
        "gmail_forwarding_addresses": settings_users * PROFILE_GMAIL_SETTINGS_WRITES_PER_USER["forwarding_address"],
        "gmail_user_delegates": settings_users * PROFILE_GMAIL_SETTINGS_WRITES_PER_USER["personal_delegate"],
        "gmail_settings_writes": settings_users * sum(PROFILE_GMAIL_SETTINGS_WRITES_PER_USER.values()),
        "gmail_settings_reads": settings_users * sum(PROFILE_GMAIL_SETTINGS_READS_PER_USER.values()),
        "gmail_settings_requests": settings_users * (
            sum(PROFILE_GMAIL_SETTINGS_WRITES_PER_USER.values())
            + sum(PROFILE_GMAIL_SETTINGS_READS_PER_USER.values())),
        "calendar_events": sum(p["calendar_events"] for p in plans),
        "secondary_calendars": sum(p["secondary_calendars"] for p in plans),
        "contacts": sum(p["contacts"] for p in plans),
        "contact_groups": sum(p["contact_groups"] for p in plans),
        "tasks": sum(p["tasks"] for p in plans),
        "task_lists": sum(p["task_lists"] for p in plans),
        "directory_groups": dept_groups,
        "directory_members": dept_members,
        "department_files": dept_files,
        "department_folders": dept_folders,
        "outside_grants": outside_grants,
    }


def budget(plans: list[dict], *, upload_bps: float, drive_per_sec: float, mail_per_sec: float, workers: int,
           pool_bytes: int, pool_files: int, max_hours: float, natural_msg_bytes: int = NATURAL_MSG_BYTES,
           seed: int = 0, per_user_per_sec: float | None = None, new_accounts: int = 0,
           fixed_users: int = 0, directory_per_sec: float = DEFAULT_DIRECTORY_REQUESTS_PER_SEC) -> dict:
    """Per-service counts and a sequential phase estimate from explicit rates.

    API payload bytes crossing this box are pool files plus raw mail; API JSON
    metadata is not counted. The upload speed is shared; Drive is tenant-wide;
    the other API rates are per active user. The sequential estimate takes no
    credit for phase overlap, so --max-hours is a conservative refusal gate.
    Mail keeps its planned counts and shrinks message sizes first; if even the
    smallest message does not fit, all mailboxes scale by one count factor.
    """
    per_user_per_sec = per_user_per_sec or mail_per_sec
    if min(upload_bps, drive_per_sec, mail_per_sec, per_user_per_sec, directory_per_sec, max_hours) <= 0:
        raise ValueError("upload, API and time budgets must be positive")
    users = len(plans)
    workers = max(1, min(workers, max(1, users)))
    # Profile mode also re-applies idempotent Gmail settings to the existing
    # cohort entries. The admin is reserved but is not impersonated/seeded.
    settings_users = users + max(0, fixed_users - 1)
    counts = service_counts(plans, seed, pool_files, settings_users=settings_users)
    # --create-users checks every cohort/profile address with users.get and
    # inserts only the new profile accounts. provision.ensure_users is serial,
    # so the Directory phase gets no user-worker speedup. fixed_users reserves
    # the admin too; the admin is not in the seed's get/create list.
    account_lookups = users + max(0, fixed_users - 1) if (new_accounts or fixed_users) else 0
    account_calls = account_lookups + new_accounts
    counts["directory_users_created"] = new_accounts
    counts["directory_user_lookups"] = account_lookups
    messages = counts["gmail_messages"]
    concurrency = max(1, min(workers, users))
    pool_upload_h = pool_bytes / upload_bps / 3600
    drive_writes = (counts["drive_files"] + counts["drive_folders"]
                    + counts["drive_permissions"] + counts["shared_drives"])
    directory_writes = counts["directory_groups"] + counts["directory_members"]
    calendar_writes = counts["calendar_events"] + counts["secondary_calendars"] * 2
    contact_writes = counts["contacts"] * 2 + counts["contact_groups"]
    task_writes = counts["tasks"] + counts["task_lists"]
    fixed_phases = {
        "account_creation": (account_calls / directory_per_sec + (20 if new_accounts else 0)) / 3600,
        "pool_upload": pool_upload_h,
        "directory_sharing": directory_writes / (per_user_per_sec * concurrency) / 3600,
        "drive": drive_writes / drive_per_sec / 3600,
        "calendar": calendar_writes / (per_user_per_sec * concurrency) / 3600,
        "gmail_settings": counts["gmail_settings_requests"] / (per_user_per_sec * concurrency) / 3600,
        "contacts": contact_writes / (per_user_per_sec * concurrency) / 3600,
        "tasks": task_writes / (per_user_per_sec * concurrency) / 3600,
    }
    gmail_fixed_writes = counts["gmail_labels"] + counts["gmail_delegates"]
    available_mail_hours = max(0.0, max_hours - sum(fixed_phases.values())
                               - gmail_fixed_writes / (mail_per_sec * concurrency) / 3600)
    available_mail_seconds = available_mail_hours * 3600
    msg_bytes, factor = natural_msg_bytes, 1.0
    if messages and available_mail_seconds > 0:
        remaining_after_mail_api = max(0.0, available_mail_seconds - messages / (mail_per_sec * concurrency))
        room = remaining_after_mail_api * upload_bps
        if messages * msg_bytes > room:
            msg_bytes = room / messages
            if msg_bytes < MIN_MSG_BYTES:
                per_message_seconds = MIN_MSG_BYTES / upload_bps + 1 / (mail_per_sec * concurrency)
                factor = min(1.0, available_mail_seconds / (messages * per_message_seconds))
                sent_at_floor = int(messages * factor)
                if sent_at_floor:
                    upload_seconds = max(0.0, available_mail_seconds - sent_at_floor / (mail_per_sec * concurrency))
                    msg_bytes = min(MIN_MSG_BYTES, upload_seconds * upload_bps / sent_at_floor)
                else:
                    msg_bytes = MIN_MSG_BYTES
    sent = min(messages, int(messages * factor))
    if factor == 1.0:
        sent = messages
    mail_bytes = int(sent * msg_bytes)
    counts["gmail_messages"] = sent
    mail_writes = sent + counts["gmail_labels"] + counts["gmail_delegates"]
    phase_hours = dict(fixed_phases)
    phase_hours["gmail"] = mail_writes / (mail_per_sec * concurrency) / 3600
    hours = sum(phase_hours.values()) + mail_bytes / upload_bps / 3600
    total_items = sum(counts[k] for k in (
        "drive_files", "drive_folders", "drive_permissions", "shared_drives",
        "gmail_messages", "gmail_labels", "gmail_delegates", "calendar_events",
        "gmail_settings_writes",
        "secondary_calendars", "contacts", "contact_groups", "tasks", "task_lists",
        "directory_groups", "directory_members", "directory_users_created"))
    return {"users": users, "fixed_users": fixed_users, "new_accounts": new_accounts,
            "files": sum(p["files"] for p in plans), "folders": sum(p["folders"] for p in plans),
            "messages_planned": messages, "messages": sent,
            "count_factor": factor, "msg_bytes": int(msg_bytes), "items": total_items,
            "items_by_service": counts, "pool_bytes": pool_bytes, "mail_bytes": mail_bytes,
            "bytes_crossing_box": pool_bytes + mail_bytes, "drive_bytes_planned": sum(p["bytes"] for p in plans),
            "hours": hours, "phase_hours": phase_hours,
            "drive_hours": phase_hours["drive"], "mail_api_hours": phase_hours["gmail"],
            "upload_hours": phase_hours["pool_upload"] + mail_bytes / upload_bps / 3600,
            "refuse": (f"{hours:.1f} h is over --max-hours {max_hours:g}" if hours > max_hours else "")}
