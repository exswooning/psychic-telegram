"""Remove what the DMS imported into a few target mailboxes, so a test can start from the same state again.

Deletes ONLY emails on the target that the ledger does not map (the engine's own copies stay), PERMANENTLY:
a trashed copy keeps its Message-ID and could make the next import skip it. Dry run unless --apply.
Refuses unless the target domain is a declared sandbox, a user is not this account's, or the full-mail
scope is not delegated to the target key (never falls back to Trash). Run as the account (MIGRATION_DB env)."""
import argparse
import sys

import domain_guard
from config import Settings
from db import MigrationDB

PURGE_SCOPE = "https://mail.google.com/"


def _client(settings, user):
    import google_auth_httplib2
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        settings.target_sa_key, scopes=[PURGE_SCOPE]).with_subject(user)
    return build("gmail", "v1", cache_discovery=False,
                 http=google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=120)))


def unmapped_ids(svc, mapped: set) -> list:
    out, tok = [], None
    while True:
        r = svc.users().messages().list(userId="me", maxResults=500, pageToken=tok, includeSpamTrash=True).execute()
        out += [m["id"] for m in r.get("messages", []) if m["id"] not in mapped]
        tok = r.get("nextPageToken")
        if not tok:
            return out


def wipe(settings, db, sources, apply=False, client=_client, say=print) -> int:
    why = domain_guard.refuse_reason(settings.target_domain, "reset")
    if why:
        say("REFUSED: " + why)
        return 2
    domain_guard.require_not_source(settings.target_domain, settings.source_domain)
    ledger = {r["source_email"].lower(): r["target_email"] for r in db.all_identities() if r["entity_type"] == "user"}
    bad = [u for u in sources if u.lower() not in ledger]
    if bad or not sources or len(sources) > 5:
        say("REFUSED: name 1-5 users of this account" + (f" (not users here: {bad})" if bad else ""))
        return 2
    mapped = {r[0] for r in db.conn.execute(
        "SELECT target_id FROM id_mapping WHERE type IN ('message','draft') AND target_id IS NOT NULL")}
    for source in sources:
        target = ledger[source.lower()]
        try:
            svc = client(settings, target)
            ids = unmapped_ids(svc, mapped)
        except Exception as exc:        # noqa: BLE001 - an ungranted scope is an answer, not a crash
            say(f"REFUSED for {target}: cannot read or delete there ({str(exc)[:160]}); no fallback to Trash")
            return 2
        say(f"{target}: {len(ids)} emails the ledger does not map" + ("" if apply else " (dry run: nothing deleted)"))
        if apply:
            for i in range(0, len(ids), 1000):
                svc.users().messages().batchDelete(userId="me", body={"ids": ids[i:i + 1000]}).execute()
            left = unmapped_ids(svc, mapped)
            say(f"{target}: deleted {len(ids) - len(left)}; {len(left)} left")
            if left:
                return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--identities", required=True, help="csv of the users (source,target) to wipe")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    from dms_migrate import identity_sources
    sources = identity_sources(args.identities) or []
    st = Settings()
    db = MigrationDB(st.db_path)
    try:
        return wipe(st, db, sources, apply=args.apply)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
