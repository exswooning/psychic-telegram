"""
tools/corpus.py
===============
Generates a realistic five-user organisation in a sandbox Drive, with the
cross-user sharing graph that makes a tenant migration interesting.

Why a realistic org rather than a pile of files
-----------------------------------------------
A flat directory of 5,000 identical PDFs tests throughput and nothing else. Real
tenants fail on *shape*: departments shared domain-wide, project folders shared
with a subset of colleagues, finance data restricted to two people, and the same
document appearing in four users' "Shared with me". That shape is what exercises
identity translation, inherited-permission handling, and — most importantly —
whether a file owned by one user gets duplicated once per person it was shared
with.

Ownership model
---------------
Each user owns their own department tree and one project, and shares outward.
That means the union of what the five users own equals the corpus exactly once,
even though every user *sees* far more than they own. With `OWNED_ONLY=true`
(the default), the migration should reproduce that union — no more.

    total target files == total source files owned
    NOT total source files visible

Verifying that equality is the whole point of seeding sharing.
"""

from __future__ import annotations

import concurrent.futures as futures
import os
import threading
import random
from datetime import datetime, timedelta, timezone

from config import FOLDER_MIME

SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
SLIDES_MIME = "application/vnd.google-apps.presentation"

# ----------------------------------------------------------------------
# Org chart. Each user owns one department and one project.
# ----------------------------------------------------------------------
ORG = [
    {"local": "alice", "dept": "Engineering", "project": "PRJ-001-Apollo"},
    {"local": "bob", "dept": "Finance", "project": "PRJ-002-Borealis"},
    {"local": "carol", "dept": "Sales", "project": "PRJ-003-Cygnus"},
    {"local": "dave", "dept": "Marketing", "project": "PRJ-004-Draco"},
    {"local": "erin", "dept": "People", "project": "PRJ-005-Eridanus"},
]

DEPT_SUBFOLDERS = {
    "Engineering": ["Architecture", "Runbooks", "Postmortems", "Design Docs",
                    "API Specs", "Onboarding"],
    "Finance": ["Budgets", "Forecasts", "Invoices", "Payroll", "Audit",
                "Board Reporting"],
    "Sales": ["Accounts", "Pipeline", "Proposals", "Contracts",
              "Competitive", "QBRs"],
    "Marketing": ["Campaigns", "Brand Assets", "Content Calendar", "Events",
                  "Analytics", "Press"],
    "People": ["Policies", "Recruiting", "Reviews", "Handbook", "Training",
               "Comp Bands"],
}

PROJECT_SUBFOLDERS = ["Discovery", "Design", "Specs", "Meeting Notes",
                      "Assets", "Status Reports"]

ACCOUNTS = ["Acme Corp", "Globex", "Initech", "Umbrella", "Soylent",
            "Hooli", "Vandelay", "Wonka Industries"]

# Sensitive departments get a restricted ACL rather than domain-wide.
RESTRICTED_DEPTS = {"Finance", "People"}

# Operator-requested extra reader on every broadly-shared folder (dept and
# project roots): granted alongside the domain-wide/team share, never in
# place of it, and never on the two paths that exist specifically to test
# *restricted* access (RESTRICTED_DEPTS, the personal folder) -- widening
# those would defeat the one thing they are there to verify.
EXTRA_EXTERNAL_SHARE = "aryan@nestnepal.com.np"

# ----------------------------------------------------------------------
# Scale profiles: files per leaf folder, archive depth, etc.
# ----------------------------------------------------------------------
SCALES = {
    "tiny":   {"per_leaf": 2,  "archive_years": 1, "wide": 30,
               "accounts": 2, "deep": 8,  "big_native": False},
    "small":  {"per_leaf": 5,  "archive_years": 2, "wide": 120,
               "accounts": 4, "deep": 12, "big_native": True},
    "medium": {"per_leaf": 14, "archive_years": 3, "wide": 300,
               "accounts": 6, "deep": 16, "big_native": True},
    "large":  {"per_leaf": 40, "archive_years": 5, "wide": 900,
               "accounts": 8, "deep": 20, "big_native": True},
    "huge":   {"per_leaf": 120, "archive_years": 7, "wide": 3000,
               "accounts": 8, "deep": 25, "big_native": True},
}


# ----------------------------------------------------------------------
# Content generators — plausible rather than random noise, so that a human
# spot-checking the target sees something recognisable.
# ----------------------------------------------------------------------
def _doc_text(title: str, dept: str, seed: int) -> bytes:
    rng = random.Random(seed)
    paras = [
        f"{title}",
        "",
        f"Owner: {dept} | Status: {rng.choice(['Draft', 'In review', 'Approved'])}",
        f"Last reviewed: {2019 + rng.randint(0, 5)}-{rng.randint(1,12):02d}",
        "",
        "Summary",
        "This document records the current position and the decisions taken. "
        "It is retained for audit purposes and reviewed annually.",
        "",
        "Detail",
    ]
    for i in range(rng.randint(6, 20)):
        paras.append(
            f"{i+1}. " + " ".join(
                rng.choice([
                    "The team agreed to proceed with the proposed approach.",
                    "Costs are tracked against the approved budget line.",
                    "Dependencies are recorded in the linked tracker.",
                    "Risks were reviewed and no new items were raised.",
                    "Action carried forward to the next review cycle.",
                    "Sign-off is pending from the accountable owner.",
                ]) for _ in range(rng.randint(1, 4))
            )
        )
    return ("\n".join(paras) + "\n").encode("utf-8")


def _sheet_csv(title: str, seed: int, rows: int = 60) -> bytes:
    rng = random.Random(seed)
    out = ["Item,Owner,Quarter,Budget,Actual,Variance,Status"]
    for i in range(rows):
        budget = rng.randint(1000, 90000)
        actual = int(budget * rng.uniform(0.7, 1.3))
        out.append(
            f"{title} line {i+1},{rng.choice(['alice','bob','carol','dave','erin'])},"
            f"Q{rng.randint(1,4)} FY{2021 + rng.randint(0,4)},{budget},{actual},"
            f"{actual - budget},{rng.choice(['Open','Closed','At risk'])}"
        )
    return ("\n".join(out) + "\n").encode("utf-8")


def _pdf_bytes(title: str, seed: int) -> bytes:
    rng = random.Random(seed)
    body = f"%PDF-1.4\n% {title}\n".encode()
    return body + os.urandom(rng.randint(20_000, 400_000))


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ----------------------------------------------------------------------
class CorpusBuilder:
    """Builds one user's slice of the org and wires up their outbound shares."""

    def __init__(self, drive, settings, user: str, peers: list[str],
                 external: str, scale: str, media_factory, retry,
                 rng_seed: int = 0):
        self.drive = drive
        self.settings = settings
        self.user = user
        self.peers = peers                # the other four, same source domain
        self.external = external
        self.cfg = SCALES[scale]
        self._media = media_factory
        self._retry = retry
        self.rng = random.Random(hash(user) & 0xFFFF if not rng_seed else rng_seed)
        # _run_leaves creates files on several threads, and every counter
        # below is a read-modify-write. Guarded rather than trusted to the
        # GIL: `d[k] += 1` is three bytecodes, not one.
        self._mlock = threading.Lock()
        self.m = {
            "folders": 0, "docs": 0, "sheets": 0, "slides": 0, "binaries": 0,
            "shortcuts": 0, "grants": {"user": 0, "domain": 0, "anyone": 0,
                                       "external": 0},
            "grants_rejected": [], "oversized_native": 0, "items": {},
            "comments": 0,
        }

    # -- thread-safe metric writes ---------------------------------------
    def _bump(self, key: str, n: int = 1) -> None:
        with self._mlock:
            self.m[key] += n

    def _bump_grant(self, kind: str) -> None:
        with self._mlock:
            self.m["grants"][kind] += 1

    def _note_rejected(self, label: str) -> None:
        with self._mlock:
            if label not in self.m["grants_rejected"]:
                self.m["grants_rejected"].append(label)

    # -- primitives ------------------------------------------------------
    def _create(self, body, media=None, fields="id,name"):
        fn = self._retry(
            lambda: self.drive.files().create(
                body=body, media_body=media, fields=fields,
                supportsAllDrives=True,
            ).execute()
        )
        return fn()

    def _grant(self, file_id: str, body: dict, kind: str) -> bool:
        try:
            fn = self._retry(
                lambda: self.drive.permissions().create(
                    fileId=file_id, body=body, sendNotificationEmail=False,
                    supportsAllDrives=True, fields="id",
                ).execute()
            )
            fn()
            self._bump_grant(kind)
            return True
        except Exception as exc:  # noqa: BLE001
            # A tenant that blocks link or external sharing is a finding, not
            # something to swallow silently.
            self._note_rejected(f"{kind}:{type(exc).__name__}")
            return False

    def folder(self, name: str, parent: str | None = None,
               days_ago: int | None = None) -> str:
        body = {"name": name, "mimeType": FOLDER_MIME,
                "parents": [parent or "root"]}
        if days_ago is not None:
            body["modifiedTime"] = _iso(days_ago)
        self._bump("folders")
        return self._create(body)["id"]

    def doc(self, name: str, parent: str, dept: str,
            seed: int | None = None) -> str:
        # seed passed in by _plan_leaf; drawn here only for direct callers.
        seed = self.rng.randint(0, 10**9) if seed is None else seed
        fid = self._create(
            {"name": name, "parents": [parent], "mimeType": DOC_MIME},
            media=self._media(_doc_text(name, dept, seed), "text/plain"),
        )["id"]
        self._bump("docs")
        return fid

    def sheet(self, name: str, parent: str, seed: int | None = None) -> str:
        seed = self.rng.randint(0, 10**9) if seed is None else seed
        fid = self._create(
            {"name": name, "parents": [parent], "mimeType": SHEET_MIME},
            media=self._media(_sheet_csv(name, seed), "text/csv"),
        )["id"]
        self._bump("sheets")
        return fid

    def slides(self, name: str, parent: str) -> str:
        # Creating a native file with no media body yields an empty Slides
        # deck. Enough to exercise the export/convert round trip.
        fid = self._create(
            {"name": name, "parents": [parent], "mimeType": SLIDES_MIME}
        )["id"]
        self._bump("slides")
        return fid

    def binary(self, name: str, parent: str, data: bytes,
               mime: str = "application/pdf",
               days_ago: int | None = None) -> str:
        body = {"name": name, "parents": [parent]}
        if days_ago is not None:
            body["modifiedTime"] = _iso(days_ago)
        self._bump("binaries")
        return self._create(body, media=self._media(data, mime))["id"]

    def comment(self, file_id: str, content: str,
                replies: tuple[str, ...] = ()) -> None:
        """
        Attach a comment thread to a file.

        Comments are the one Drive feature that cannot be migrated with the
        original author intact -- the API has no way to write a comment *as*
        somebody else -- so seeding them is what makes that fidelity loss
        visible in a rehearsal rather than a surprise after cutover.
        """
        try:
            fn = self._retry(
                lambda: self.drive.comments().create(
                    fileId=file_id, body={"content": content}, fields="id",
                ).execute()
            )
            created = fn()
            self._bump("comments")
            for r in replies:
                try:
                    self._retry(lambda body=r, cid=created["id"]:
                                self.drive.replies().create(
                                    fileId=file_id, commentId=cid,
                                    body={"content": body}, fields="id",
                                ).execute())()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            self._note_rejected(f"comment:{type(exc).__name__}")

    # -- sharing helpers --------------------------------------------------
    def share_domain(self, file_id: str, role: str = "reader") -> None:
        self._grant(file_id, {"type": "domain", "role": role,
                              "domain": self.settings.source_domain,
                              "allowFileDiscovery": True}, "domain")

    def share_users(self, file_id: str, emails: list[str], role: str) -> None:
        for e in emails:
            self._grant(file_id, {"type": "user", "role": role,
                                  "emailAddress": e}, "user")

    def share_external(self, file_id: str, role: str = "commenter") -> None:
        self._grant(file_id, {"type": "user", "role": role,
                              "emailAddress": self.external}, "external")

    def share_specific_external(self, file_id: str, email: str,
                                role: str = "reader") -> None:
        """A named external grant distinct from self.external (the fixed
        rehearsal address every build already carries) -- for an operator-
        requested address that needs access to broadly-shared content
        without being folded into the standard external-sharing test case."""
        self._grant(file_id, {"type": "user", "role": role,
                              "emailAddress": email}, "external")

    def share_anyone(self, file_id: str) -> None:
        self._grant(file_id, {"type": "anyone", "role": "reader",
                              "allowFileDiscovery": False}, "anyone")

    # ==================================================================
    def build(self, dept: str, project: str, edge_cases: bool) -> dict:
        cfg = self.cfg
        root = self.folder("MIGRATION-TEST", days_ago=500)
        self.m["items"]["root"] = root

        self._build_department(root, dept)
        self._build_project(root, project)
        self._build_archive(root, dept)
        self._build_personal(root)
        if edge_cases:
            self._build_edge_cases(root)
        else:
            self._build_light_edge_cases(root)

        self.m["total_files"] = (self.m["docs"] + self.m["sheets"]
                                 + self.m["slides"] + self.m["binaries"]
                                 + self.m["shortcuts"])
        return self.m

    # -- department -------------------------------------------------------
    def _build_department(self, root: str, dept: str) -> None:
        cfg = self.cfg
        dept_root = self.folder(f"Dept-{dept}", root, days_ago=450)
        self.m["items"]["dept_root"] = dept_root

        # Restricted departments go to named colleagues only; the rest are
        # readable domain-wide. This is the ACL shape real orgs actually have.
        if dept in RESTRICTED_DEPTS:
            self.share_users(dept_root, self.peers[:2], "reader")
        else:
            self.share_domain(dept_root, "reader")
            # Domain-wide already covers every org member (>= 10 with the
            # tenant's real headcount); adds the one operator-requested
            # external reader on top. Skipped for RESTRICTED_DEPTS on
            # purpose -- see EXTRA_EXTERNAL_SHARE's comment.
            self.share_specific_external(dept_root, EXTRA_EXTERNAL_SHARE)

        for sub in DEPT_SUBFOLDERS[dept]:
            sub_id = self.folder(sub, dept_root, days_ago=self.rng.randint(30, 400))

            # Sales gets a per-account fan-out, which produces the wide,
            # shallow shape that real CRM-adjacent drives have.
            leaves = [sub_id]
            if dept == "Sales" and sub == "Accounts":
                leaves = [self.folder(a, sub_id,
                                      days_ago=self.rng.randint(20, 300))
                          for a in ACCOUNTS[: cfg["accounts"]]]

            for leaf in leaves:
                self._leaf_files(leaf, dept, sub, cfg["per_leaf"])

    def _plan_leaf(self, parent: str, dept: str, sub: str, i: int) -> dict:
        """Draw every random decision for one leaf file, making no API call.

        Split from _exec_leaf so the API calls can run concurrently while
        the randomness stays strictly serial. That ordering is the whole
        point: `self.rng` is a single Random consumed in a fixed sequence,
        so drawing from it inside worker threads would both race and change
        which file gets which content. Planning here, in the original
        order, keeps a seeded run byte-identical to the serial version --
        only the timing of the writes changes.
        """
        r = self.rng.random()
        age = self.rng.randint(5, 900)
        plan: dict = {"parent": parent, "age": age}
        if r < 0.34:
            plan.update(kind="doc", name=f"{sub} — {dept} note {i+1:03d}",
                        dept=dept, seed=self.rng.randint(0, 10**9))
        elif r < 0.60:
            plan.update(kind="sheet", name=f"{sub} tracker {i+1:03d}",
                        seed=self.rng.randint(0, 10**9))
        elif r < 0.68:
            plan.update(kind="slides", name=f"{sub} deck {i+1:03d}")
        elif r < 0.88:
            plan.update(kind="binary", name=f"{sub} report {i+1:03d}.pdf",
                        data=_pdf_bytes(sub, self.rng.randint(0, 10**9)),
                        mime="application/pdf")
        else:
            ext, mime = self.rng.choice([
                ("png", "image/png"), ("jpg", "image/jpeg"),
                ("csv", "text/csv"), ("json", "application/json"),
                ("zip", "application/zip"),
            ])
            plan.update(kind="binary", name=f"{sub} asset {i+1:03d}.{ext}",
                        data=os.urandom(self.rng.randint(5_000, 250_000)),
                        mime=mime)

        # ~12% of files carry a comment thread. Real Drives are full of
        # them, and they are the clearest example of a migration that
        # "succeeds" while quietly losing something users care about.
        if self.rng.random() < 0.12:
            plan["comment"] = self.rng.choice([
                "Can we get sign-off on this before Friday?",
                "Numbers in row 12 look off to me.",
                "Superseded by the Q3 version — keeping for reference.",
                "Approved. Nice work.",
            ])
            plan["replies"] = (("Agreed, updating now.",)
                               if self.rng.random() < 0.5 else ())

        # ~18% of individual files carry their own grant on top of whatever
        # they inherit — the messy reality that inherited-ACL logic must handle.
        r2 = self.rng.random()
        if r2 < 0.13:
            plan["share"] = ("users", self.rng.choice(self.peers),
                             self.rng.choice(["writer", "commenter"]))
        elif r2 < 0.16:
            plan["share"] = ("external", None, None)
        elif r2 < 0.18:
            plan["share"] = ("anyone", None, None)
        return plan

    def _exec_leaf(self, plan: dict) -> None:
        """Perform one planned leaf's API calls. Runs on a worker thread."""
        parent, kind = plan["parent"], plan["kind"]
        if kind == "doc":
            fid = self.doc(plan["name"], parent, plan["dept"], seed=plan["seed"])
        elif kind == "sheet":
            fid = self.sheet(plan["name"], parent, seed=plan["seed"])
        elif kind == "slides":
            fid = self.slides(plan["name"], parent)
        else:
            fid = self.binary(plan["name"], parent, plan["data"],
                              plan["mime"], days_ago=plan["age"])

        if "comment" in plan:
            self.comment(fid, plan["comment"], replies=plan["replies"])

        if "share" in plan:
            how, who, role = plan["share"]
            if how == "users":
                self.share_users(fid, [who], role)
            elif how == "external":
                self.share_external(fid)
            else:
                self.share_anyone(fid)

    def _leaf_workers(self) -> int:
        """How many leaves to create at once. 1 unless explicitly opted in.

        `httplib2.Http` is not thread-safe, and CorpusBuilder captures one
        `self.drive` for the whole user, so every worker here would drive
        the same socket. That corrupts glibc's heap -- SIGABRT, "free():
        invalid next size (normal)", no Python traceback. Reproduced live:
        29 users started, 0 finished, dead in 9 seconds. drive_engine.py
        documents the identical failure at its `src` property and fixes it
        by resolving the client per access from AuthManager's per-thread
        cache rather than holding it on the instance; the seeder needs the
        same before this can default above 1.

        SEED_LEAF_WORKERS exists so that fix can be verified in place.
        """
        return max(1, int(os.getenv("SEED_LEAF_WORKERS", "1")))

    def _run_leaves(self, plans: list[dict]) -> None:
        """Create planned leaves concurrently, bounded by drive_file_workers.

        Each leaf is 1-3 API calls that spend nearly all their time waiting
        on a round trip, so running them one at a time leaves most of the
        account's write budget unused: measured on the live tenant at 0.47
        Drive writes/sec against Google's 3/sec per-account ceiling -- a
        6.3x gap that no amount of extra *user* workers can close, because
        that ceiling is per account and one user is one thread.

        This does not raise the ceiling. Every call still passes through
        the same per-user limiter, so the threads interleave into one
        bucket; it raises utilisation of a ceiling we sit far below. Same
        reasoning, and the same knob, as drive_file_workers in the
        migration engine -- see its comment in config.py, which describes
        this identical finding on the migrate side.
        """
        # See _leaf_workers: 1 until the seeder holds per-thread clients.
        n = self._leaf_workers()
        if n == 1 or len(plans) <= 1:
            for p in plans:
                self._exec_leaf(p)
            return
        with futures.ThreadPoolExecutor(max_workers=n) as pool:
            for fut in futures.as_completed(
                    [pool.submit(self._exec_leaf, p) for p in plans]):
                # Surface a genuine bug; a per-item API failure has already
                # been recorded into self.m by the primitive that raised.
                fut.result()

    def _leaf_files(self, parent: str, dept: str, sub: str, count: int) -> None:
        """Plan `count` leaves serially, then create them concurrently.

        Chunked rather than planned all at once: a plan for a binary file
        carries its bytes (up to 250 KB), so holding every plan for a large
        folder in memory at once would undo the point of sizing worker
        pools against RAM. A window of 4x the worker count is enough to
        keep every thread fed.
        """
        n = self._leaf_workers()
        window, batch = max(4, n * 4), []
        for i in range(count):
            batch.append(self._plan_leaf(parent, dept, sub, i))
            if len(batch) >= window:
                self._run_leaves(batch)
                batch = []
        if batch:
            self._run_leaves(batch)

    def _leaf_file(self, parent: str, dept: str, sub: str, i: int) -> None:
        """One leaf, planned and created inline. Kept for callers that
        create a single file outside a counted loop."""
        self._exec_leaf(self._plan_leaf(parent, dept, sub, i))

    # -- project ----------------------------------------------------------
    def _build_project(self, root: str, project: str) -> None:
        cfg = self.cfg
        proj_root = self.folder(project, root, days_ago=200)
        self.m["items"]["project_root"] = proj_root

        # A project team: this user plus three of the four peers, as writers.
        team = self.peers[:3]
        self.m["items"]["project_team"] = team
        self.share_users(proj_root, team, "writer")
        # Plus one commenter, so not every grant is the same role.
        if len(self.peers) > 3:
            self.share_users(proj_root, [self.peers[3]], "commenter")
        # Everyone else in the org gets read access too -- additive to the
        # writer/commenter subset above, not a replacement for it -- so the
        # project folder reaches every org member (>= 10 with the tenant's
        # real headcount), plus the one operator-requested external reader.
        if len(self.peers) > 4:
            self.share_users(proj_root, self.peers[4:], "reader")
        self.share_specific_external(proj_root, EXTRA_EXTERNAL_SHARE)

        for sub in PROJECT_SUBFOLDERS:
            sub_id = self.folder(sub, proj_root,
                                 days_ago=self.rng.randint(10, 200))
            self._leaf_files(sub_id, "Project", sub,
                             max(2, cfg["per_leaf"] // 2))

        # A deck the whole domain can see, plus an externally-shared spec —
        # the two shapes that trip target-tenant sharing policy.
        deck = self.slides(f"{project} — Executive Readout", proj_root)
        self.share_domain(deck, "reader")
        spec = self.doc(f"{project} — Vendor Spec", proj_root, "Project")
        self.share_external(spec, "commenter")
        self.m["items"]["external_shared_doc"] = spec

    # -- archive ----------------------------------------------------------
    def _build_archive(self, root: str, dept: str) -> None:
        cfg = self.cfg
        arch = self.folder("Archive", root, days_ago=800)
        this_year = datetime.now(timezone.utc).year
        for y in range(this_year - cfg["archive_years"], this_year):
            y_folder = self.folder(str(y), arch, days_ago=(this_year - y) * 365)
            for q in range(1, 5):
                q_folder = self.folder(f"Q{q}", y_folder)
                for i in range(max(1, cfg["per_leaf"] // 3)):
                    self.binary(f"{dept} {y} Q{q} statement {i+1}.pdf", q_folder,
                                _pdf_bytes("archive", self.rng.randint(0, 10**9)),
                                days_ago=(this_year - y) * 365 - q * 60)

    # -- personal (deliberately unshared) ---------------------------------
    def _build_personal(self, root: str) -> None:
        p = self.folder("Personal", root, days_ago=100)
        self.m["items"]["personal_root"] = p
        for i in range(max(2, self.cfg["per_leaf"] // 2)):
            self.doc(f"Personal note {i+1}", p, "Personal")
        self.sheet("Personal expenses", p)
        # No grants here at all. Post-migration, this folder must still have
        # none — a migration that leaks private files is worse than one that
        # drops them.

    # -- edge cases --------------------------------------------------------
    def _build_edge_cases(self, root: str) -> None:
        cfg = self.cfg
        edge = self.folder("99-Edge-Cases", root)
        self.m["items"]["edge_root"] = edge

        # Deep chain
        node = edge
        for i in range(cfg["deep"]):
            node = self.folder(f"depth-{i:02d}", node)
        deep_file = self.binary("bottom-of-the-well.pdf", node,
                                b"%PDF-1.4 deep\n", days_ago=300)
        self.m["items"]["deep_leaf"] = deep_file

        # Wide folder — pagination
        wide = self.folder("wide-folder", edge)
        for i in range(cfg["wide"]):
            self.binary(f"bulk-{i:04d}.txt", wide, f"row {i}\n".encode(),
                        "text/plain")
        self.m["wide_count"] = cfg["wide"]

        # Hostile names
        odd = self.folder("odd names", edge)
        for name in ['re/port "final" (v2).pdf', "café — résumé.pdf",
                     "trailing space .pdf", "emoji \U0001F600 file.pdf",
                     "a" * 120 + ".pdf", "..dotfile.pdf", "NUL~lock.pdf"]:
            self.binary(name, odd, b"%PDF-1.4 odd\n")

        # Boundary sizes
        sizes = self.folder("sizes", edge)
        self.binary("zero-byte.dat", sizes, b"", "application/octet-stream")
        self.binary("just-under-chunk.bin", sizes, os.urandom(15 * 1024 * 1024 - 1),
                    "application/octet-stream")
        self.binary("just-over-chunk.bin", sizes, os.urandom(17 * 1024 * 1024),
                    "application/octet-stream")

        # A large native Doc, for exercising the export/import round trip.
        #
        # NOT the 10 MB files.export ceiling: Google's plain-text-to-Docs
        # import conversion hard-fails (400 Bad Request) somewhere between
        # ~1.3 MB and ~2 MB of source text -- confirmed empirically -- which
        # is well below the 10 MB the resulting export would need to exceed
        # to trigger SKIPPED_EXPORT_TOO_LARGE. The two constraints can't both
        # be hit through a single plain-text upload; 460_000 repetitions
        # (~12.9 MB source) reliably crashed the whole build with an
        # unhandled 400. 50_000 reps (~1.3 MB) is comfortably inside the
        # import limit. Actually exceeding the export ceiling would need
        # building the doc incrementally via the Docs API (batchUpdate),
        # which needs a separate `documents` OAuth scope this tool doesn't
        # request.
        if cfg["big_native"]:
            self._create(
                {"name": "Oversized Doc", "parents": [edge], "mimeType": DOC_MIME},
                media=self._media(b"Lorem ipsum dolor sit amet.\n" * 50_000,
                                  "text/plain"),
            )
            self._bump("docs")
            self.m["oversized_native"] = 1

        # Every ACL shape on one file
        acl_file = self.binary("shared-every-way.pdf", edge, b"%PDF-1.4 acl\n")
        self.m["items"]["acl_file"] = acl_file
        self.share_users(acl_file, [self.peers[0]], "writer")
        self.share_external(acl_file, "commenter")
        self.share_domain(acl_file, "reader")
        self.share_anyone(acl_file)

        # Inherited-permission folder: grants live on the folder, not the files
        inh = self.folder("inherited-acl", edge)
        self.m["items"]["inherited_folder"] = inh
        self.share_users(inh, [self.peers[1]], "reader")
        for i in range(3):
            self.binary(f"inherits-{i}.pdf", inh, b"%PDF-1.4 inh\n")

        # Shortcut into the deep tree
        sc = self._create({
            "name": "shortcut-to-deep-file", "mimeType": SHORTCUT_MIME,
            "parents": [edge], "shortcutDetails": {"targetId": deep_file},
        })
        self.m["shortcuts"] += 1
        self.m["items"]["shortcut"] = sc["id"]

        self._delta_targets(edge)

    def _build_light_edge_cases(self, root: str) -> None:
        """Every user gets a few, so failures are not concentrated on user 1."""
        edge = self.folder("99-Edge-Cases", root)
        self.m["items"]["edge_root"] = edge
        for name in ['re/port "final" (v2).pdf', "café — résumé.pdf",
                     "a" * 120 + ".pdf"]:
            self.binary(name, edge, b"%PDF-1.4 odd\n")
        self.binary("zero-byte.dat", edge, b"", "application/octet-stream")
        acl_file = self.binary("shared-every-way.pdf", edge, b"%PDF-1.4 acl\n")
        self.m["items"]["acl_file"] = acl_file
        self.share_users(acl_file, [self.peers[0]], "writer")
        self.share_external(acl_file, "commenter")
        self.share_domain(acl_file, "reader")
        self._delta_targets(edge)

    def _delta_targets(self, parent: str) -> None:
        """Three known files the rehearsal edits to prove the delta pass works."""
        d = self.folder("delta-targets", parent)
        self.m["items"]["delta_files"] = [
            self.binary(f"delta-{i}.txt", d, f"version 1 of {i}\n".encode(),
                        "text/plain", days_ago=200 + i)
            for i in range(3)
        ]
