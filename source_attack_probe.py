#!/usr/bin/env python3
"""Red-team the source tenant's outbound write boundary.

The test suite imports the discovery-matrix helpers below and swaps only the
HTTP transport. The CLI builds the same AuthManager clients with real
credentials. Its live mode rewrites each generated write request into a
description update to one operator-named Drive file, then restores and checks
the original metadata.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote, urlsplit

import httplib2
from googleapiclient import discovery_cache
from googleapiclient.discovery import fix_method_name
from googleapiclient.http import HttpRequest, MediaInMemoryUpload

import auth


SAFE_LIVE_DOMAIN = "a.sarafgloabalexim.com"
STAGING_ID = "SOURCE_ATTACK_PROBE_STAGING"
OUTSIDE_ID = "SOURCE_ATTACK_PROBE_OUTSIDE"
PROBE_USER = "source-attack-probe@example.invalid"


@dataclass(frozen=True)
class MethodCase:
    """One non-read discovery method as reachable through an AuthManager route."""

    route: str
    api: str
    version: str
    resource_path: tuple[str, ...]
    method_name: str
    method_doc: dict[str, Any]

    @property
    def operation(self) -> str:
        path = ".".join((*self.resource_path, self.method_name))
        return f"{self.api}.{path}" if path else f"{self.api}.{self.method_name}"

    @property
    def id(self) -> str:
        return f"{self.route} :: {self.operation}"


@dataclass
class RequestRecord:
    method: str
    url: str
    body: str
    headers: dict[str, str]


class RecorderTransport:
    """httplib2-shaped fake that records the exact outbound request."""

    def __init__(self):
        self.requests: list[RequestRecord] = []
        self.redirect_codes = {301, 302, 303, 307, 308}

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            return value
        if hasattr(value, "read"):
            data = value.read()
            return RecorderTransport._text(data)
        return str(value)

    def request(self, uri, method="GET", body=None, headers=None, **_kwargs):
        header_map = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        text = self._text(body)
        self.requests.append(RequestRecord(str(method).upper(), str(uri), text, header_map))

        response_headers = {"status": "200", "content-type": "application/json"}
        if "uploadType=resumable" in str(uri):
            response_headers["location"] = "https://upload.example.invalid/session/1"
        return httplib2.Response(response_headers), b"{}"

    def close(self):
        return None


class DummyCredentials:
    def with_subject(self, _user):
        return self


def bundled_document(api: str, version: str) -> dict[str, Any]:
    raw = discovery_cache.get_static_doc(api, version)
    if raw is None:
        raise RuntimeError(f"googleapiclient has no bundled discovery document for {api} {version}")
    return json.loads(raw)


def walk_methods(doc: dict[str, Any], prefix: tuple[str, ...] = ()):
    for method_name, method_doc in (doc.get("methods") or {}).items():
        yield prefix, method_name, method_doc
    for resource_name, child in (doc.get("resources") or {}).items():
        yield from walk_methods(child, (*prefix, resource_name))


def source_discovery_specs() -> tuple[tuple[str, str], ...]:
    """All source clients built in production plus its admin/identity shorthands."""
    return tuple((name, version) for name, version in auth._API_VERSIONS.items()) + (
        ("admin", "directory_v1"),
        ("cloudidentity", "v1"),
    )


def attack_matrix() -> list[MethodCase]:
    """Every non-GET/HEAD method under each production source-client route."""
    routes: list[tuple[str, set[str]]] = []
    for api in auth._API_VERSIONS:
        routes.extend(((f"api('source', {api!r}, user)", {api}),
                       (f"_service('source', {api!r}, user)", {api})))
    routes.extend((
        ("source_drive()", {"drive"}),
        ("allow_copy_into(source_drive(), staging, moves=False)", {"drive"}),
        ("allow_copy_into(source_drive(), staging, moves=True)", {"drive"}),
        ("source_directory()", {"admin"}),
        ('directory("source", writable=False)', {"admin"}),
        ('directory("source", writable=True)', {"admin"}),
        ('directory("source", groups=True)', {"admin"}),
        ('cloud_identity("source")', {"cloudidentity"}),
    ))

    cases: list[MethodCase] = []
    for route, apis in routes:
        for api, version in source_discovery_specs():
            if api not in apis:
                continue
            doc = bundled_document(api, version)
            for resource_path, method_name, method_doc in walk_methods(doc):
                if method_doc.get("httpMethod", "GET").upper() in {"GET", "HEAD"}:
                    continue
                cases.append(MethodCase(route, api, version, resource_path,
                                        method_name, method_doc))
    return cases


def parameter_value(name: str, spec: dict[str, Any]) -> Any:
    kind = spec.get("type", "string")
    if kind == "integer":
        return 1
    if kind == "number":
        return 1
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    pattern = spec.get("pattern")
    if pattern:
        # Google discovery documents encode resource-name shape as a regular
        # expression. Substitute each path segment with a harmless token so
        # discovery's local argument validation accepts the probe request.
        candidate = re.sub(r"\[\^/\]\+", "probe", pattern.strip("^$"))
        if candidate and not any(ch in candidate for ch in "[]()"):
            return candidate
    values = {
        "userId": "me",
        "fileId": "SOURCE_ATTACK_FILE",
        "spreadsheetId": "SOURCE_ATTACK_FILE",
        "documentId": "SOURCE_ATTACK_FILE",
        "presentationId": "SOURCE_ATTACK_FILE",
        "formId": "SOURCE_ATTACK_FILE",
        "customer": "my_customer",
        "customerId": "my_customer",
        "customerKey": "my_customer",
        "parent": "customers/my_customer",
        "name": "groups/source-attack-probe",
        "resourceName": "people/me",
        "range": "A1",
        "mimeType": "text/plain",
        "pageToken": "SOURCE_ATTACK_TOKEN",
    }
    return values.get(name, "SOURCE_ATTACK_PROBE")


def request_kwargs(case: MethodCase, *, staging: bool = False) -> dict[str, Any]:
    method_doc = case.method_doc
    parameters = method_doc.get("parameters") or {}
    kwargs = {name: parameter_value(name, spec)
              for name, spec in parameters.items() if spec.get("required")}
    path_names = set(re.findall(r"\{\+?([^}]+)\}", method_doc.get("path", "")))
    for name in path_names:
        kwargs.setdefault(name, parameter_value(name, parameters.get(name, {})))

    if method_doc.get("request"):
        body: dict[str, Any] = {}
        if case.api == "drive" and case.resource_path == ("files",) and case.method_name == "copy":
            body["parents"] = [STAGING_ID if staging else OUTSIDE_ID]
        elif case.api == "drive" and case.resource_path == ("files",) and case.method_name == "update":
            body = {"description": "source attack probe"}
        kwargs["body"] = body
    return kwargs


def nested_resource(service, path: Iterable[str]):
    result = service
    for name in path:
        result = getattr(result, name)()
    return result


def generated_request(service, case: MethodCase, *, staging: bool = False,
                      media: bool = False):
    resource = nested_resource(service, case.resource_path)
    kwargs = request_kwargs(case, staging=staging)
    if media and case.method_doc.get("supportsMediaUpload"):
        kwargs["media_body"] = MediaInMemoryUpload(b"probe", "text/plain", resumable=False)
    elif case.method_doc.get("supportsMediaUpload") and case.method_doc.get("request"):
        # Exercise the metadata-only shape where a media upload is optional.
        kwargs["media_body"] = None
    return getattr(resource, fix_method_name(case.method_name))(**kwargs)


def route_specs(manager, user: str):
    """Return clients with the same public production constructors."""
    specs: list[tuple[str, str, Callable[[], Any]]] = []
    for api in auth._API_VERSIONS:
        specs.append((f"api('source', {api!r}, user)", api,
                      lambda api=api: manager.api("source", api, user)))
        specs.append((f"_service('source', {api!r}, user)", api,
                      lambda api=api: manager._service("source", api, user)))
    specs.extend((
        ("source_drive()", "drive", lambda: manager.source_drive(user)),
        ("allow_copy_into(source_drive(), staging, moves=False)", "drive",
         lambda: auth.allow_copy_into(manager.source_drive(user), STAGING_ID, moves=False)),
        ("allow_copy_into(source_drive(), staging, moves=True)", "drive",
         lambda: auth.allow_copy_into(manager.source_drive(user), STAGING_ID, moves=True)),
        ("source_directory()", "admin", manager.source_directory),
        ('directory("source", writable=False)', "admin",
         lambda: manager.directory("source", writable=False)),
        ('directory("source", writable=True)', "admin",
         lambda: manager.directory("source", writable=True)),
        ('directory("source", groups=True)', "admin",
         lambda: manager.directory("source", groups=True)),
        ('cloud_identity("source")', "cloudidentity",
         lambda: manager.cloud_identity("source")),
    ))
    return specs


def _service_route(manager, case: MethodCase, user: str):
    if case.route.startswith("api("):
        return manager.api("source", case.api, user)
    if case.route.startswith("_service("):
        return manager._service("source", case.api, user)
    if case.route == "source_drive()":
        return manager.source_drive(user)
    if case.route.startswith("allow_copy_into("):
        moves = "moves=True" in case.route
        return auth.allow_copy_into(manager.source_drive(user), STAGING_ID, moves=moves)
    if case.route == "source_directory()":
        return manager.source_directory()
    if case.route == 'directory("source", writable=False)':
        return manager.directory("source", writable=False)
    if case.route == 'directory("source", writable=True)':
        return manager.directory("source", writable=True)
    if case.route == 'directory("source", groups=True)':
        return manager.directory("source", groups=True)
    if case.route == 'cloud_identity("source")':
        return manager.cloud_identity("source")
    raise KeyError(case.route)


def is_allowed_source_write(record: RequestRecord, staging: str = STAGING_ID) -> bool:
    """The migration's two narrow source Drive write exceptions."""
    method, url = record.method.upper(), record.url
    body = record.body
    if method == "POST" and re.search(r"/drive/v3/files/[^/?]+/copy(?:\?|$)", url):
        try:
            return json.loads(body or "{}").get("parents") == [staging]
        except (json.JSONDecodeError, AttributeError):
            return False
    if method == "PATCH" and re.search(r"/drive/v3/files/[^/?]+(?:\?|$)", url):
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            return False
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        allowed_parameters = {"addParents", "removeParents", "supportsAllDrives", "fields", "alt"}
        return (
            not payload
            and set(query) <= allowed_parameters
            and query.get("addParents") == [staging]
            and bool(query.get("removeParents", [""])[0])
        )
    if "/batch/" in url:
        inner = re.findall(r"(?im)^(GET|HEAD|POST|PUT|PATCH|DELETE)\s+([^\r\n]+)", body)
        if not inner:
            return False
        return all(inner_method.upper() in {"GET", "HEAD"}
                   for inner_method, _inner_uri in inner)
    return False


def leaking_requests(records: Iterable[RequestRecord]) -> list[RequestRecord]:
    """Unapproved writes, excluding read-only POSTs and the exact copy/move exceptions."""
    leaks = []
    for record in records:
        if record.method.upper() in {"GET", "HEAD"}:
            continue
        parts = urlsplit(record.url)
        if (record.method.upper() == "POST"
                and any(pattern.search(parts.path) for pattern in auth._READ_POSTS)):
            continue
        if "/batch/" in record.url and is_allowed_source_write(record):
            continue
        if is_allowed_source_write(record):
            continue
        token_refresh = (
            record.method.upper() == "POST"
            and parts.netloc.lower() in {"oauth2.googleapis.com", "www.googleapis.com", "accounts.google.com"}
            and parts.path in {"/token", "/oauth2/v4/token", "/o/oauth2/token"}
        )
        if token_refresh:
            continue
        leaks.append(record)
    return leaks


def _drive_file_uri(file_id: str) -> str:
    return ("https://www.googleapis.com/drive/v3/files/" + quote(file_id, safe="")
            + "?fields=id%2Cdescription%2CmodifiedTime")


def mutate_request_to_description_update(request, file_id: str, description: str,
                                         etag: str | None = None):
    """Turn any generated request into the CLI's one reversible, safe write."""
    request.method = "PATCH"
    request.uri = _drive_file_uri(file_id)
    request.body = json.dumps({"description": description}, separators=(",", ":"))
    request.headers = {"content-type": "application/json"}
    if etag:
        request.headers["if-match"] = etag
    # Clear upload state too: a resumable upload must not survive the mutation.
    request.resumable = None
    return request


def _json_result(response) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    if isinstance(response, bytes):
        response = response.decode("utf-8", errors="replace")
    if isinstance(response, str):
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return {}
    return {}


def _http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "resp", None)
    if response is not None:
        try:
            return int(response.status)
        except (AttributeError, TypeError, ValueError):
            pass
    match = re.search(r"(?:HTTP|status)[^0-9]*(\d{3})", str(exc), re.I)
    return int(match.group(1)) if match else None


def _direct_drive_client(manager, user: str):
    client = manager.api("source", "drive", user)
    return client._svc if isinstance(client, auth.ReadOnlyDrive) else client


def _fetch_original(client, file_id: str) -> dict[str, Any]:
    response, content = client._http.request(
        _drive_file_uri(file_id), method="GET", headers={"accept": "application/json"})
    status = int(response.status)
    if status >= 400:
        raise RuntimeError(f"Drive metadata read failed with HTTP {status}")
    value = _json_result(content)
    value["etag"] = response.get("etag")
    if not isinstance(value, dict) or not value.get("id"):
        raise RuntimeError("Drive returned no file metadata; refusing the live probe")
    return value


def _direct_metadata(manager, user: str, file_id: str) -> dict[str, Any]:
    creds = manager._credentials("source", user)
    http = auth.google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
    response, content = http.request(
        _drive_file_uri(file_id), method="GET", headers={"accept": "application/json"})
    status = int(response.status)
    if status >= 400:
        raise RuntimeError(f"Drive metadata read failed with HTTP {status}")
    value = _json_result(content)
    value["etag"] = response.get("etag")
    if not value.get("id"):
        raise RuntimeError("Drive returned no file metadata")
    return value


def _restore_with_direct_transport(manager, user: str, file_id: str,
                                   original: dict[str, Any],
                                   expected_description: str | None = None) -> tuple[bool, str]:
    """Restore outside Bitport's guard in case the probe exposed a bypass."""
    try:
        current = _direct_metadata(manager, user, file_id)
        if (expected_description is not None
                and current.get("description") != expected_description):
            return False, "restore refused: probe marker no longer matches the file description"
        creds = manager._credentials("source", user)
        http = auth.google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
        body = {"description": original.get("description"),
                "modifiedTime": original.get("modifiedTime")}
        request = HttpRequest(http=http, postproc=lambda _resp, content: content,
                              uri=_drive_file_uri(file_id), method="PATCH",
                              body=json.dumps(body, separators=(",", ":")),
                              headers={"content-type": "application/json",
                                       **({"if-match": current["etag"]}
                                          if current.get("etag") else {})})
        request.execute()
        verify_response, verify_content = http.request(
            _drive_file_uri(file_id), method="GET", headers={"accept": "application/json"})
        if int(verify_response.status) >= 400:
            return False, f"restore verification read failed with HTTP {verify_response.status}"
        current = _json_result(verify_content)
        if current.get("description") != original.get("description"):
            return False, "restore verification failed: description differs"
        if (original.get("modifiedTime") and current.get("modifiedTime")
                and current.get("modifiedTime") != original.get("modifiedTime")):
            return False, "restore verification failed: modifiedTime differs"
        return True, "restored and verified"
    except Exception as exc:  # noqa: BLE001 - report the cleanup failure
        return False, f"restore failed: {type(exc).__name__}: {exc}"


def validate_cli_args(args: argparse.Namespace) -> None:
    if args.domain.strip().lower().rstrip(".") != SAFE_LIVE_DOMAIN:
        raise ValueError(f"source probe refuses domain {args.domain!r}; only {SAFE_LIVE_DOMAIN} is allowed")
    user_domain = args.user.rsplit("@", 1)[-1].strip().lower().rstrip(".") if "@" in args.user else ""
    if user_domain != SAFE_LIVE_DOMAIN:
        raise ValueError(f"source probe requires --user in {SAFE_LIVE_DOMAIN}")
    if args.live:
        if not args.i_understand:
            raise ValueError("--live requires --i-understand")
        if not args.file:
            raise ValueError("--live requires --file with one source Drive file ID")
    elif args.i_understand or args.file:
        raise ValueError("--file and --i-understand are only accepted with --live")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="build real source clients and print the full matrix without executing requests")
    mode.add_argument("--live", action="store_true",
                      help="run every generated write as a reversible description update")
    parser.add_argument("--domain", required=True, help="must be a.sarafgloabalexim.com")
    parser.add_argument("--user", required=True, help="delegated source user (and source admin for admin APIs)")
    parser.add_argument("--file", help="one source Drive file ID, required for --live")
    parser.add_argument("--i-understand", action="store_true",
                        help="required for live writes; confirms the named file may be briefly edited")
    return parser


def _build_real_manager(domain: str, user: str):
    from config import Settings

    settings = Settings()
    settings.source_domain = domain
    settings.source_admin = user
    manager = auth.AuthManager(settings)
    return manager


def run_dry_run(manager, user: str, stream=sys.stdout) -> int:
    cases = attack_matrix()
    failures = 0
    # Build every route once, including domain-guard decisions, using real
    # credential configuration. No .execute() call is made in this mode.
    clients: dict[str, Any] = {}
    for route, api, factory in route_specs(manager, user):
        if route in clients:
            continue
        try:
            clients[route] = factory()
        except Exception as exc:  # noqa: BLE001 - report what the route would do
            clients[route] = exc
    print(f"DRY-RUN: {len(cases)} discovery write methods across {len(clients)} source routes", file=stream)
    for case in cases:
        client = clients.get(case.route)
        if isinstance(client, BaseException):
            print(f"WOULD ATTEMPT {case.id} -> refused locally while building route: {client}", file=stream)
            continue
        staging = case.route.startswith("allow_copy_into(")
        try:
            request = generated_request(client, case, staging=staging,
                                        media=bool(case.method_doc.get("supportsMediaUpload")))
            body = getattr(request, "body", None)
            body_text = "" if body is None else repr(body)
            if len(body_text) > 240:
                body_text = body_text[:237] + "..."
            media_note = " media-upload" if case.method_doc.get("supportsMediaUpload") else ""
            print(f"WOULD ATTEMPT {case.id} -> {request.method} {request.uri}"
                  f" body={body_text or '<empty>'}{media_note}; live mode rewrites this "
                  "to one description PATCH and restores it", file=stream)
        except auth.SourceWriteRefused as exc:
            print(f"WOULD ATTEMPT {case.id} -> refused locally: {exc}", file=stream)
        except Exception as exc:  # noqa: BLE001
            print(f"WOULD ATTEMPT {case.id} -> request construction failed: {type(exc).__name__}: {exc}", file=stream)
            failures += 1
    for route in ("source_drive()", "api('source', 'drive', user)"):
        try:
            client = clients[route]
            raw = client._svc if isinstance(client, auth.ReadOnlyDrive) else client
            raw.new_batch_http_request()
            print(f"WOULD ATTEMPT {route} :: batch write and batch GET controls", file=stream)
        except Exception as exc:  # noqa: BLE001
            print(f"WOULD ATTEMPT {route} :: batch -> refused locally: {exc}", file=stream)
    for control in ("source client _http.request", "mutated HttpRequest", "MediaUpload",
                    "resumable upload"):
        print(f"WOULD ATTEMPT {control}; live mode rewrites it to one description PATCH and restores it", file=stream)
    return 1 if failures else 0


def run_live(manager, user: str, file_id: str, stream=sys.stdout) -> int:
    drive = _direct_drive_client(manager, user)
    original = _fetch_original(drive, file_id)
    cases = attack_matrix()
    clients: dict[str, Any] = {}
    for route, _api, factory in route_specs(manager, user):
        if route not in clients:
            try:
                clients[route] = factory()
            except Exception as exc:  # noqa: BLE001
                clients[route] = exc

    attempted = 0
    errors = 0
    cleanup_failed = False
    for case in cases:
        client = clients.get(case.route)
        if isinstance(client, BaseException):
            print(f"{case.id}: refused-locally ({client})", file=stream)
            continue
        try:
            request = generated_request(
                client, case,
                staging=case.route.startswith("allow_copy_into("),
                media=bool(case.method_doc.get("supportsMediaUpload")))
        except auth.SourceWriteRefused as exc:
            print(f"{case.id}: refused-locally ({exc})", file=stream)
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"{case.id}: request-construction-failed ({type(exc).__name__}: {exc})", file=stream)
            errors += 1
            continue

        attempted += 1
        marker = "bitport-source-guard-probe-" + uuid.uuid4().hex[:12]
        current = _direct_metadata(manager, user, file_id)
        mutate_request_to_description_update(request, file_id, marker,
                                            current.get("etag"))
        result_status = "WENT THROUGH"
        updated = False
        attempt_error = None
        try:
            request.execute()
            updated = True
        except auth.SourceWriteRefused as exc:
            result_status = f"refused-locally ({exc})"
        except Exception as exc:  # noqa: BLE001
            attempt_error = exc
            if _http_status(exc) == 403:
                result_status = f"refused-by-Google (403 scope/permission; {exc})"
            else:
                result_status = f"request-error ({type(exc).__name__}: {exc})"
                errors += 1
        status = _http_status(attempt_error) if attempt_error is not None else None
        if updated or (result_status.startswith("request-error")
                       and (status is None or status >= 500)):
            ok, detail = _restore_with_direct_transport(manager, user, file_id, original,
                                                        expected_description=marker)
            if not ok:
                errors += 1
                cleanup_failed = True
                result_status += f"; {detail}"
        print(f"{case.id}: {result_status}", file=stream)
        if cleanup_failed:
            print("aborting remaining probes because file restoration was not verified", file=stream)
            return 1

    # Explicit transport and batch probes are separate from the generated
    # discovery-method matrix above. They also use only the same reversible
    # description update on --file.
    raw_drive = _direct_drive_client(manager, user)
    current = _direct_metadata(manager, user, file_id)
    marker = "bitport-source-guard-probe-" + uuid.uuid4().hex[:12]
    raw_status = "WENT THROUGH"
    raw_updated = False
    raw_uncertain = False
    try:
        response, _content = raw_drive._http.request(
            _drive_file_uri(file_id), method="PATCH",
            body=json.dumps({"description": marker}, separators=(",", ":")),
            headers={"content-type": "application/json",
                     **({"if-match": current["etag"]} if current.get("etag") else {})},
        )
        status = int(response.status)
        raw_updated = status < 400
        if status == 403:
            raw_status = "refused-by-Google (403 scope/permission)"
        elif status >= 400:
            raw_status = f"request-error (HTTP {status})"
            errors += 1
            raw_uncertain = status >= 500
    except auth.SourceWriteRefused as exc:
        raw_status = f"refused-locally ({exc})"
    except Exception as exc:  # noqa: BLE001
        status = _http_status(exc)
        if status == 403:
            raw_status = f"refused-by-Google (403 scope/permission; {exc})"
        else:
            raw_status = f"request-error ({type(exc).__name__}: {exc})"
            errors += 1
            raw_uncertain = status is None or status >= 500
    if not raw_updated and raw_uncertain:
        try:
            raw_updated = _direct_metadata(manager, user, file_id).get("description") == marker
        except Exception as exc:  # noqa: BLE001
            errors += 1
            cleanup_failed = True
            raw_status += f"; could not check for probe marker ({type(exc).__name__}: {exc})"
    if raw_updated:
        ok, detail = _restore_with_direct_transport(manager, user, file_id, original,
                                                    expected_description=marker)
        if not ok:
            errors += 1
            cleanup_failed = True
            raw_status += f"; {detail}"
    print(f"raw _http.request: {raw_status}", file=stream)
    attempted += 1
    if cleanup_failed:
        print("aborting remaining probes because file restoration was not verified", file=stream)
        return 1

    # new_batch_http_request is deliberately reached from the guarded wrapper;
    # the nested request is rewritten to the named file's description update.
    source = manager.source_drive(user)
    raw_source = source._svc if isinstance(source, auth.ReadOnlyDrive) else source
    read_results: list[tuple[Any, Any]] = []
    try:
        read_batch = source.new_batch_http_request(
            callback=lambda _request_id, response, exception: read_results.append((response, exception)))
        read_batch.add(raw_source.files().get(
            fileId=file_id, fields="id,description,modifiedTime"))
        read_batch.execute()
        read_error = next((exception for _response, exception in read_results if exception), None)
        if read_error is not None:
            print(f"batch GET: refused-by-Google/route ({read_error})", file=stream)
        else:
            print("batch GET: read went through", file=stream)
    except Exception as exc:  # noqa: BLE001
        print(f"batch GET: request-error ({type(exc).__name__}: {exc})", file=stream)
        errors += 1

    batch_marker = "bitport-source-guard-probe-" + uuid.uuid4().hex[:12]
    current = _direct_metadata(manager, user, file_id)
    callback_results: list[tuple[Any, Any]] = []
    batch_updated = False
    batch_status = "WENT THROUGH"
    try:
        batch_request = raw_source.files().update(
            fileId=file_id, body={"description": batch_marker})
        mutate_request_to_description_update(batch_request, file_id, batch_marker,
                                             current.get("etag"))

        def _record_batch(_request_id, response, exception):
            callback_results.append((response, exception))

        batch = source.new_batch_http_request(callback=_record_batch)
        batch.add(batch_request)
        batch.execute()
        for response, exception in callback_results:
            if exception is not None:
                if _http_status(exception) == 403:
                    batch_status = f"refused-by-Google (403 scope/permission; {exception})"
                else:
                    batch_status = f"request-error ({type(exception).__name__}: {exception})"
                    errors += 1
            elif response is not None:
                batch_updated = True
    except auth.SourceWriteRefused as exc:
        batch_status = f"refused-locally ({exc})"
    except Exception as exc:  # noqa: BLE001
        status = _http_status(exc)
        if status == 403:
            batch_status = f"refused-by-Google (403 scope/permission; {exc})"
        else:
            batch_status = f"request-error ({type(exc).__name__}: {exc})"
            errors += 1
    if not batch_updated and (batch_status == "WENT THROUGH"
                              or batch_status.startswith("request-error")):
        try:
            batch_updated = (_direct_metadata(manager, user, file_id).get("description")
                             == batch_marker)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            cleanup_failed = True
            batch_status += f"; could not check for probe marker ({type(exc).__name__}: {exc})"
    if batch_updated:
        ok, detail = _restore_with_direct_transport(manager, user, file_id, original,
                                                    expected_description=batch_marker)
        if not ok:
            errors += 1
            cleanup_failed = True
            batch_status += f"; {detail}"
    print(f"batch write: {batch_status}", file=stream)
    attempted += 1
    if cleanup_failed:
        print("file restoration was not verified", file=stream)
        return 1

    print(f"Live summary: attempted {attempted}; errors {errors}; file {file_id}", file=stream)
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate_cli_args(args)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    manager = _build_real_manager(args.domain, args.user)
    if args.dry_run:
        return run_dry_run(manager, args.user)
    return run_live(manager, args.user, args.file)


if __name__ == "__main__":
    raise SystemExit(main())
