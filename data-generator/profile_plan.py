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

# The profile has no figure for these; they are the plan's own, stated here and nowhere else.
LINK_KINDS = {"own": 0.40, "colleague": 0.25, "shared_drive": 0.10, "folder": 0.10, "deleted": 0.05, "outside": 0.10}
DUPLICATE_NAME = 0.02       # a file named exactly like the one before it, in the same folder
PATH_BREAKING = 0.015       # a name with a character that breaks a path
LONG_NAME = 0.03
DEVANAGARI = 0.25
MIN_MSG_BYTES = 1024        # a header block and a one-line body: the smallest message the generator makes
NATURAL_MSG_BYTES = 1_700_000   # 245 GB over 141,416 messages (the plan's table; the profile holds no mail bytes)

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


def plan(profile: dict, users: int, seed: int) -> list[dict]:
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
        kinds = _split(links, LINK_KINDS)
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
        })
    return out


def _split(total: int, shares: dict) -> dict:
    """total divided by shares in whole numbers that add up to total (largest remainders)."""
    raw = {k: total * v for k, v in shares.items()}
    out = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[: total - sum(out.values())]:
        out[k] += 1
    return out


def scale_messages(plans: list[dict], factor: float) -> list[dict]:
    """One factor on every mailbox's message count (and what is counted inside it)."""
    if factor >= 1:
        return plans
    for p in plans:
        p["messages"] = int(p["messages"] * factor)
        p["threads"] = min(p["threads"], p["messages"]) if p["messages"] else 0
        p["link_mail"] = min(p["link_mail"], p["messages"])
        p["link_kinds"] = _split(p["link_mail"], LINK_KINDS)
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


def budget(plans: list[dict], *, upload_bps: float, drive_per_sec: float, mail_per_sec: float, workers: int,
           pool_bytes: int, pool_files: int, max_hours: float, natural_msg_bytes: int = NATURAL_MSG_BYTES) -> dict:
    """Items, bytes that cross the box and hours, from rates measured elsewhere. Drive is one tenant-wide budget
    (files.copy, folders, shortcuts); mail is per mailbox, so mailboxes in parallel divide it; the upload line is shared.
    Mail keeps the profile's counts and shrinks its sizes to fit the window; if even the smallest message does not fit,
    one factor scales every mailbox's count. `refuse` names why the window cannot hold the plan."""
    users = len(plans)
    drive_items = sum(p["files"] + p["folders"] for p in plans)
    messages = sum(p["messages"] for p in plans)
    window = max_hours * 3600
    pool_t = pool_bytes / upload_bps
    drive_t = (drive_items + pool_files) / drive_per_sec
    room = max(0.0, window - pool_t) * upload_bps                    # bytes the line can carry for mail
    msg_bytes, factor = natural_msg_bytes, 1.0
    if messages and messages * msg_bytes > room:
        msg_bytes = room / messages
        if msg_bytes < MIN_MSG_BYTES:
            factor, msg_bytes = room / (messages * MIN_MSG_BYTES), MIN_MSG_BYTES
    sent = int(messages * factor)
    mail_api_t = sent / (mail_per_sec * max(1, min(workers, users)))
    mail_bytes = int(sent * msg_bytes)
    hours = (pool_t + max(drive_t, mail_api_t, mail_bytes / upload_bps)) / 3600
    return {"users": users, "files": sum(p["files"] for p in plans), "folders": sum(p["folders"] for p in plans),
            "messages_planned": messages, "messages": sent, "count_factor": factor, "msg_bytes": int(msg_bytes),
            "items": drive_items + sent + pool_files, "pool_bytes": pool_bytes, "mail_bytes": mail_bytes,
            "bytes_crossing_box": pool_bytes + mail_bytes, "drive_bytes_planned": sum(p["bytes"] for p in plans),
            "hours": hours, "drive_hours": drive_t / 3600, "mail_api_hours": mail_api_t / 3600,
            "upload_hours": (pool_t + mail_bytes / upload_bps) / 3600,
            "refuse": (f"{hours:.1f} h is over --max-hours {max_hours:g}" if hours > max_hours else "")}
