"""One parser for the ledger's grant keys (`audit_log.item_id` of item_type 'acl'), plan item 5.

The engine writes, per source file id `<fid>`:
  <fid>:<email>            a person or group
  <fid>:anyone             anyone with the link
  <fid>:domain:<domain>    everyone in a domain
  <fid>:(no-email)         a dangling source grant with no address
  <fid>:(list-failed)      the source's permissions could not be read
Split at the FIRST colon: a check that split at the last one read `<fid>:domain` as a file id and
reported a file that does not exist (9 Nov 2026).
"""
from __future__ import annotations

from typing import NamedTuple


class Grant(NamedTuple):
    file_id: str
    kind: str          # person | anyone | domain | no-email | list-failed | unknown
    who: str           # the address, the domain, or "" for anyone / markers


def parse_grant(item_id: str) -> Grant:
    fid, _, rest = (item_id or "").partition(":")
    r = rest.strip()
    if r == "anyone":
        return Grant(fid, "anyone", "")
    if r.startswith("domain:"):
        return Grant(fid, "domain", r[7:].lower())
    if r == "(no-email)":
        return Grant(fid, "no-email", "")
    if r == "(list-failed)":
        return Grant(fid, "list-failed", "")
    if "@" in r:
        return Grant(fid, "person", r.lower())
    return Grant(fid, "unknown", r.lower())
