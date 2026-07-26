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

import os
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
        self.m = {
            "folders": 0, "docs": 0, "sheets": 0, "slides": 0, "binaries": 0,
            "shortcuts": 0, "grants": {"user": 0, "domain": 0, "anyone": 0,
                                       "external": 0},
            "grants_rejected": [], "oversized_native": 0, "items": {},
        }

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
            self.m["grants"][kind] += 1
            return True
        except Exception as exc:  # noqa: BLE001
            # A tenant that blocks link or external sharing is a finding, not
            # something to swallow silently.
            label = f"{kind}:{type(exc).__name__}"
            if label not in self.m["grants_rejected"]:
                self.m["grants_rejected"].append(label)
            return False

    def folder(self, name: str, parent: str | None = None,
               days_ago: int | None = None) -> str:
        body = {"name": name, "mimeType": FOLDER_MIME,
                "parents": [parent or "root"]}
        if days_ago is not None:
            body["modifiedTime"] = _iso(days_ago)
        self.m["folders"] += 1
        return self._create(body)["id"]

    def doc(self, name: str, parent: str, dept: str) -> str:
        seed = self.rng.randint(0, 10**9)
        fid = self._create(
            {"name": name, "parents": [parent], "mimeType": DOC_MIME},
            media=self._media(_doc_text(name, dept, seed), "text/plain"),
        )["id"]
        self.m["docs"] += 1
        return fid

    def sheet(self, name: str, parent: str) -> str:
        seed = self.rng.randint(0, 10**9)
        fid = self._create(
            {"name": name, "parents": [parent], "mimeType": SHEET_MIME},
            media=self._media(_sheet_csv(name, seed), "text/csv"),
        )["id"]
        self.m["sheets"] += 1
        return fid

    def slides(self, name: str, parent: str) -> str:
        # Creating a native file with no media body yields an empty Slides
        # deck. Enough to exercise the export/convert round trip.
        fid = self._create(
            {"name": name, "parents": [parent], "mimeType": SLIDES_MIME}
        )["id"]
        self.m["slides"] += 1
        return fid

    def binary(self, name: str, parent: str, data: bytes,
               mime: str = "application/pdf",
               days_ago: int | None = None) -> str:
        body = {"name": name, "parents": [parent]}
        if days_ago is not None:
            body["modifiedTime"] = _iso(days_ago)
        self.m["binaries"] += 1
        return self._create(body, media=self._media(data, mime))["id"]

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
                for i in range(cfg["per_leaf"]):
                    self._leaf_file(leaf, dept, sub, i)

    def _leaf_file(self, parent: str, dept: str, sub: str, i: int) -> None:
        r = self.rng.random()
        age = self.rng.randint(5, 900)
        if r < 0.34:
            fid = self.doc(f"{sub} — {dept} note {i+1:03d}", parent, dept)
        elif r < 0.60:
            fid = self.sheet(f"{sub} tracker {i+1:03d}", parent)
        elif r < 0.68:
            fid = self.slides(f"{sub} deck {i+1:03d}", parent)
        elif r < 0.88:
            fid = self.binary(f"{sub} report {i+1:03d}.pdf", parent,
                              _pdf_bytes(sub, self.rng.randint(0, 10**9)),
                              days_ago=age)
        else:
            kind = self.rng.choice([
                ("png", "image/png"), ("jpg", "image/jpeg"),
                ("csv", "text/csv"), ("json", "application/json"),
                ("zip", "application/zip"),
            ])
            fid = self.binary(f"{sub} asset {i+1:03d}.{kind[0]}", parent,
                              os.urandom(self.rng.randint(5_000, 250_000)),
                              kind[1], days_ago=age)

        # ~18% of individual files carry their own grant on top of whatever
        # they inherit — the messy reality that inherited-ACL logic must handle.
        r2 = self.rng.random()
        if r2 < 0.13:
            self.share_users(fid, [self.rng.choice(self.peers)],
                             self.rng.choice(["writer", "commenter"]))
        elif r2 < 0.16:
            self.share_external(fid)
        elif r2 < 0.18:
            self.share_anyone(fid)

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

        for sub in PROJECT_SUBFOLDERS:
            sub_id = self.folder(sub, proj_root,
                                 days_ago=self.rng.randint(10, 200))
            for i in range(max(2, cfg["per_leaf"] // 2)):
                self._leaf_file(sub_id, "Project", sub, i)

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

        # The 10 MB files.export ceiling
        if cfg["big_native"]:
            self._create(
                {"name": "Oversized Doc", "parents": [edge], "mimeType": DOC_MIME},
                media=self._media(b"Lorem ipsum dolor sit amet.\n" * 460_000,
                                  "text/plain"),
            )
            self.m["docs"] += 1
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
