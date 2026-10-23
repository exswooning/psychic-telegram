"""Source-backed Directory profiles. Reads source; writes mapped target metadata only.

Account controls, passwords, aliases, OUs, custom schemas and photos are separate
migration concerns, explicitly reported rather than labelled profile-identical.
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import logging
import os
from datetime import datetime, timezone

# Never expand this to a complete User payload: it contains security controls.
COLLECTIONS = {
    "addresses": "type customType streetAddress extendedAddress poBox locality region postalCode country countryCode formatted primary",
    "organizations": "name title primary type customType department symbol costCenter location description domain fullTimeEquivalent",
    "phones": "value type customType primary",
    "externalIds": "value type customType",
    "relations": "value type customType",
    "languages": "languageCode customLanguage preference",
    "locations": "type customType area buildingId deskCode floorName floorSection",
    "websites": "value type customType primary",
    "keywords": "value type customType",
    "ims": "im protocol customProtocol type customType primary",
}
OBJECTS = {"gender": "type addressMeAs customGender", "notes": "value contentType"}
NAME_FIELDS = ("givenName", "familyName", "displayName")
PROFILE_FIELDS = (
    "name",
    *COLLECTIONS,
    *OBJECTS,
    "emails",
    "includeInGlobalAddressList",
)
SEPARATE_FIELDS = (
    "aliases",
    "nonEditableAliases",
    "orgUnitPath",
    "customSchemas",
    "thumbnailPhotoUrl",
)
READ_FIELDS = ",".join(
    ("id", "primaryEmail", "etag", *PROFILE_FIELDS, *SEPARATE_FIELDS)
)
log = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _address(value):
    return str(value or "").strip().lower()


def validate_pairs(pairs, settings):
    pairs = [(_address(s), _address(t)) for s, t in pairs]
    if len({s for s, _ in pairs}) != len(pairs) or len({t for _, t in pairs}) != len(
        pairs
    ):
        raise ValueError("Profile sync requires unambiguous one-to-one user mappings")
    for source, target in pairs:
        if (
            source.count("@") != 1
            or target.count("@") != 1
            or source.rsplit("@", 1)[1] != _address(settings.source_domain)
            or target.rsplit("@", 1)[1] != _address(settings.target_domain)
            or source == target
        ):
            raise ValueError(
                "Profile mapping is outside the configured source/target pair"
            )
    return pairs


def profile_body(user, target_email, mapping):
    """Preserve spelling/case/Unicode exactly; translate only recorded email identities."""
    name = user.get("name") or {}
    if not isinstance(name, dict) or any(
        k not in name for k in ("givenName", "familyName")
    ):
        raise ValueError(
            "Source Directory name is unavailable; refusing to invent a name"
        )
    out = {"name": {k: name.get(k, "") for k in NAME_FIELDS}}
    if any(not isinstance(v, str) for v in out["name"].values()):
        raise ValueError("Source name fields must be strings")
    for field, allowed in COLLECTIONS.items():
        out[field] = [
            {k: copy.deepcopy(v) for k, v in item.items() if k in allowed.split()}
            for item in user.get(field, []) or []
        ]
    for field, allowed in OBJECTS.items():
        out[field] = {
            k: copy.deepcopy(v)
            for k, v in (user.get(field) or {}).items()
            if k in allowed.split()
        }
    for relation in out["relations"]:
        if "value" in relation:
            relation["value"] = mapping.get(
                _address(relation["value"]), relation["value"]
            )
    out["emails"] = []
    # Directory repeats tenant-managed aliases in emails. Alias resources and
    # non-editable Google test aliases are separate from editable profile mail.
    # Replaying these as contact addresses cannot recreate their tenant identity.
    aliases = {_address(a) for field in ("aliases", "nonEditableAliases")
               for a in user.get(field, []) or []}
    for email in user.get("emails", []) or []:
        if (_address(email.get("address")) in aliases and not email.get("primary")
                and _address(email.get("address")) != _address(user.get("primaryEmail"))):
            continue
        row = {
            k: copy.deepcopy(v)
            for k, v in email.items()
            if k in ("address", "type", "customType", "primary")
        }
        if row.get("primary") or _address(row.get("address")) == _address(
            user.get("primaryEmail")
        ):
            row["address"] = target_email
            row["primary"] = True
        else:
            row["address"] = mapping.get(
                _address(row.get("address")), row.get("address")
            )
        out["emails"].append(row)
    if not any(e.get("primary") for e in out["emails"]):
        out["emails"].append({"address": target_email, "primary": True})
    out["includeInGlobalAddressList"] = user.get("includeInGlobalAddressList", True)
    return out


def _canonical(value):
    if isinstance(value, list):
        return sorted(
            (_canonical(x) for x in value), key=lambda x: json.dumps(x, sort_keys=True)
        )
    if isinstance(value, dict):
        return {
            k: _canonical(v)
            for k, v in sorted(value.items())
            if v not in (None, "", [], {})
        }
    return value


def differences(expected, actual):
    # Project target through the same allow-list, without remapping its addresses.
    current = profile_body(actual, actual["primaryEmail"], {})
    return [
        k for k in PROFILE_FIELDS if _canonical(expected[k]) != _canonical(current[k])
    ]


def read_users(directory, domain):
    users, token = {}, None
    while True:
        response = (
            directory.users()
            .list(
                domain=domain,
                projection="full",
                viewType="admin_view",
                maxResults=500,
                pageToken=token,
                fields=f"nextPageToken,users({READ_FIELDS})",
            )
            .execute(num_retries=3)
        )
        for user in response.get("users", []):
            email = _address(user.get("primaryEmail"))
            if email in users:
                raise ValueError("Directory returned duplicate primary identities")
            users[email] = user
        token = response.get("nextPageToken")
        if not token:
            return users


def _save(db, source, target, result):
    result["checkedAt"] = _now()
    with db.write() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS directory_profile_checks (
            source_user TEXT PRIMARY KEY,target_user TEXT NOT NULL,checked_at TEXT NOT NULL,
            status TEXT NOT NULL,payload TEXT NOT NULL)""")
        c.execute(
            "INSERT OR REPLACE INTO directory_profile_checks VALUES(?,?,?,?,?)",
            (
                source,
                target,
                _now(),
                result["status"],
                json.dumps(result, ensure_ascii=False),
            ),
        )


def latest(db):
    if not db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='directory_profile_checks'"
    ).fetchone():
        return []
    return [
        json.loads(r[0])
        for r in db.conn.execute(
            "SELECT payload FROM directory_profile_checks ORDER BY source_user"
        )
    ]


def sync_profiles(
    auth, db, settings, pairs, *, apply=False, create_missing=False, progress=print
):
    pairs = validate_pairs(pairs, settings)
    all_pairs = [
        (r["source_email"], r["target_email"])
        for r in db.all_identities()
        if r["entity_type"] == "user"
    ]
    mapping = dict(validate_pairs(all_pairs, settings))
    mapping.update(pairs)
    out = {
        "created": [],
        "existing": [],
        "failed": [],
        "profiles": [],
        "checkedAt": _now(),
        "scope": "supported Directory profile fields; separate tenant/security fields are not certified",
    }
    try:
        source_directory = auth.directory("source")  # Always read-only.
        target_directory = auth.directory("target")
        source_users = read_users(source_directory, settings.source_domain)
        target_users = read_users(target_directory, settings.target_domain)
    except Exception as exc:
        for source, target in pairs:
            row = {
                "source": source,
                "target": target,
                "status": "ERROR",
                "changedFields": [],
                "remainingFields": [],
                "separateReview": list(SEPARATE_FIELDS),
                "error": str(exc)[:350],
            }
            _save(db, source, target, row)
            out["profiles"].append(row)
            out["failed"].append((target, row["error"]))
        return out
    writer = None
    for source, target in pairs:
        row = {
            "source": source,
            "target": target,
            "status": "ERROR",
            "changedFields": [],
            "remainingFields": [],
            "separateReview": list(SEPARATE_FIELDS),
            "error": None,
        }
        try:
            user = source_users.get(source)
            if not user:
                raise ValueError(
                    "Source primary identity is missing from the full Directory inventory"
                )
            wanted = profile_body(user, target, mapping)
            row["sourceName"] = copy.deepcopy(wanted["name"])
            current = target_users.get(target)
            if current is None:
                if not create_missing:
                    raise ValueError(
                        "Target account does not exist; provision it before profile reconciliation"
                    )
                row["changedFields"] = list(PROFILE_FIELDS)
                if not apply:
                    row["status"] = "WOULD_CREATE"
                    out["created"].append((target, "(dry run — not created)"))
                else:
                    from domain_guard import require_not_source
                    import provision

                    require_not_source(settings.target_domain, settings.source_domain)
                    writer = writer or auth.directory("target", writable=True)
                    created = provision.ensure_users(
                        writer,
                        [target],
                        profiles={target: wanted},
                        require_profiles=True,
                    )
                    if created["failed"]:
                        raise RuntimeError(created["failed"][0][1])
                    out["created"].extend(created["created"])
                    current = (
                        target_directory.users()
                        .get(
                            userKey=target,
                            projection="full",
                            viewType="admin_view",
                            fields=READ_FIELDS,
                        )
                        .execute(num_retries=3)
                    )
            else:
                out["existing"].append(target)
            if current is not None:
                if _address(current.get("primaryEmail")) != target or not current.get(
                    "id"
                ):
                    raise ValueError(
                        "Target response is not the exact mapped primary identity"
                    )
                row["targetName"] = copy.deepcopy(current.get("name") or {})
                changed = differences(wanted, current)
                row["changedFields"] = changed
                row["status"] = "WOULD_UPDATE" if changed and not apply else "MATCHED"
                if changed and apply:
                    from domain_guard import require_not_source

                    require_not_source(settings.target_domain, settings.source_domain)
                    writer = writer or auth.directory("target", writable=True)
                    # update supports partial bodies and clearing repeated fields.
                    body = {
                        k: wanted[k] if wanted[k] not in ([], {}) else None
                        for k in changed
                    }
                    if not current.get("etag"):
                        raise ValueError("Target profile version is unavailable; compare again before updating")
                    request = writer.users().update(userKey=current["id"], body=body)
                    request.headers["If-Match"] = current["etag"]
                    request.execute(num_retries=3)
                    fresh = (
                        target_directory.users()
                        .get(
                            userKey=current["id"],
                            projection="full",
                            viewType="admin_view",
                            fields=READ_FIELDS,
                        )
                        .execute(num_retries=3)
                    )
                    if (
                        _address(fresh.get("primaryEmail")) != target
                        or fresh.get("id") != current["id"]
                    ):
                        raise ValueError(
                            "Destination identity changed during profile update"
                        )
                    row["targetName"] = copy.deepcopy(fresh.get("name") or {})
                    row["remainingFields"] = differences(wanted, fresh)
                    row["status"] = (
                        "UPDATED" if not row["remainingFields"] else "NEEDS_ATTENTION"
                    )
                    if row["remainingFields"]:
                        raise ValueError(
                            "Profile read-back still differs: "
                            + ", ".join(row["remainingFields"])
                        )
            progress(
                f"user profile: {source} -> {target}: {row['status']}; fields={','.join(row['changedFields']) or 'none'}"
            )
        except Exception as exc:
            row.update(status="ERROR", error=str(exc)[:350])
            out["failed"].append((target, row["error"]))
            progress(f"user profile: {source} -> {target}: ERROR: {row['error']}")
        _save(db, source, target, row)
        out["profiles"].append(row)
    return out


def export_csv(rows):
    """Human review export, not an account/password import template."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "Source email",
            "Target email",
            "Source first name",
            "Source last name",
            "Source display name",
            "Target first name",
            "Target last name",
            "Status",
            "Changed fields",
        ]
    )

    def safe(value):
        value = str(value or "")
        return (
            "'" + value
            if value.startswith(("=", "+", "-", "@", "\t", "\r", "\n"))
            else value
        )

    for row in rows:
        source, target = row.get("sourceName", {}), row.get("targetName", {})
        writer.writerow(
            [
                safe(v)
                for v in (
                    row["source"],
                    row["target"],
                    source.get("givenName"),
                    source.get("familyName"),
                    source.get("displayName"),
                    target.get("givenName"),
                    target.get("familyName"),
                    row["status"],
                    ", ".join(row["changedFields"]),
                )
            ]
        )
    return output.getvalue()


def main(argv=None):
    from config import Settings
    from db import MigrationDB
    from auth import AuthManager
    import job_admission

    parser = argparse.ArgumentParser(
        description="Compare or sync mapped user profiles; source stays read-only"
    )
    parser.add_argument("--account-id", type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--launcher-admitted", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--user", action="append")
    args = parser.parse_args(argv)
    settings = Settings(account_id=args.account_id)
    db = MigrationDB(settings.db_path)
    pairs = [
        (r["source_email"], r["target_email"])
        for r in db.all_identities()
        if r["entity_type"] == "user"
        and (not args.user or r["source_email"] in args.user)
    ]
    if not pairs:
        parser.error("No mapped users selected")
    admitted, detail = job_admission.adopt_or_admit(
        args.account_id, "user-profiles", os.getpid()
    )
    if not admitted:
        parser.error(detail)
    try:
        result = sync_profiles(
            AuthManager(settings), db, settings, pairs, apply=args.apply
        )
    finally:
        if not args.launcher_admitted:
            job_admission.release(args.account_id, "user-profiles")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
