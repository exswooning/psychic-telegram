"""Pinned Microsoft endpoints, certificate authentication and bounded Graph retries."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import random
import tempfile
import time
from urllib.parse import unquote, urlsplit
import uuid

from .catalog import CLOUDS
from .store import account_dir


class GraphError(RuntimeError):
    def __init__(self, status, code):
        self.status = status
        self.code = str(code)[:120]
        super().__init__(f"Microsoft API {status}: {self.code}")


class Stopped(RuntimeError):
    pass


def guid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("tenant and application IDs must be Microsoft GUIDs") from None


def validate_config(config):
    if config.get("mode") not in ("preserve_source", "native_move"):
        raise ValueError("choose preserve_source or native_move")
    if type(config.get("sandbox")) is not bool:
        raise ValueError("sandbox must be true or false")
    out = {"mode": config["mode"], "sandbox": config["sandbox"]}
    for side in ("source", "target"):
        raw = config.get(side, {})
        cloud = raw.get("cloud", "commercial")
        if cloud not in CLOUDS:
            raise ValueError("unknown Microsoft cloud")
        out[side] = {"tenantId": guid(raw.get("tenantId")), "clientId": guid(raw.get("clientId")),
                     "cloud": cloud, "region": str(raw.get("region", "")).strip()[:120]}
    if out["source"]["tenantId"] == out["target"]["tenantId"]:
        raise ValueError("source and target must be different tenants")
    return out


def credential_path(account_id, side):
    if side not in ("source", "target"):
        raise ValueError("unknown tenant side")
    return account_dir(account_id) / f"{side}.credential.enc"


def _fernet(account_id):
    from cryptography.fernet import Fernet
    directory = account_dir(account_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "vault.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    if path.stat().st_mode & 0o077:
        raise ValueError("Microsoft vault key must be accessible only to the Bitport service user")
    return Fernet(path.read_bytes())


def install_certificate(account_id, side, tenant_id, client_id, private_key_path, certificate_path):
    """CLI only: never accept arbitrary server paths or private keys in a web request."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    key = Path(private_key_path).read_bytes()
    parsed_key = serialization.load_pem_private_key(key, password=None)
    cert = x509.load_pem_x509_certificate(Path(certificate_path).read_bytes())
    if parsed_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
        raise ValueError("certificate does not match the private key")
    expiry = getattr(cert, "not_valid_after_utc", None)
    if expiry is None:
        expiry = cert.not_valid_after.replace(tzinfo=timezone.utc)
    if expiry <= datetime.now(timezone.utc):
        raise ValueError("certificate has expired")
    payload = {"tenantId": guid(tenant_id), "clientId": guid(client_id),
               "private_key": key.decode(), "thumbprint": cert.fingerprint(hashes.SHA1()).hex(),
               "expiresAt": expiry.isoformat()}
    destination = credential_path(account_id, side)
    data = _fernet(account_id).encrypt(json.dumps(payload).encode())
    fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".credential-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, destination)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_credential(account_id, side, connection):
    path = credential_path(account_id, side)
    if not path.exists():
        raise ValueError(f"install the {side} application certificate on the Bitport host first")
    data = json.loads(_fernet(account_id).decrypt(path.read_bytes()))
    if (data["tenantId"], data["clientId"]) != (connection["tenantId"], connection["clientId"]):
        raise ValueError(f"the installed {side} certificate belongs to a different connection")
    if datetime.fromisoformat(data["expiresAt"]) <= datetime.now(timezone.utc):
        raise ValueError(f"the {side} application certificate has expired")
    return data


def credential_status(account_id, side, connection):
    if not connection.get("tenantId"):
        return {"ready": False, "detail": "Connection not configured"}
    try:
        value = read_credential(account_id, side, connection)
        return {"ready": True, "expiresAt": value["expiresAt"], "detail": "Certificate installed; API access is checked during discovery"}
    except ImportError:
        return {"ready": False, "detail": "Install requirements-microsoft.txt on this host"}
    except (OSError, ValueError, KeyError):
        return {"ready": False, "detail": "Certificate missing, expired, unreadable or for another connection"}
    except Exception as exc:
        # Cryptography errors must not reveal encrypted data or file contents.
        if type(exc).__name__ == "InvalidToken":
            return {"ready": False, "detail": "Certificate vault cannot be decrypted on this host"}
        raise


class CertificateToken:
    def __init__(self, account_id, side, connection):
        try:
            import msal
        except ImportError:
            raise ValueError("install requirements-microsoft.txt on the Bitport host") from None
        credential = read_credential(account_id, side, connection)
        cloud = CLOUDS[connection["cloud"]]
        self.scope = cloud["graph"] + "/.default"
        self.app = msal.ConfidentialClientApplication(
            connection["clientId"], authority=cloud["authority"] + "/" + connection["tenantId"],
            client_credential={"private_key": credential["private_key"], "thumbprint": credential["thumbprint"]})

    def __call__(self):
        result = self.app.acquire_token_for_client(scopes=[self.scope])
        if not result.get("access_token"):
            raise GraphError(401, result.get("error", "authentication_failed"))
        return result["access_token"]


def retry_delay(value, attempt):
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            seconds = min(2 ** attempt + random.random(), 60)
    return max(0.1, seconds)


class GraphClient:
    def __init__(self, cloud, token, *, side, writable=False, check=None, session=None):
        import requests
        if side not in ("source", "target"):
            raise ValueError("unknown tenant side")
        if writable and side != "target":
            raise ValueError("source Graph writes are prohibited")
        self.base = CLOUDS[cloud]["graph"] + "/v1.0"
        self.side, self.writable, self.token = side, writable, token
        self.session = session or requests.Session()
        self.check = check or (lambda: None)

    def url(self, path):
        url = path if path.startswith("https://") else self.base + "/" + path.lstrip("/")
        parsed, base = urlsplit(url), urlsplit(self.base)
        if (parsed.scheme != "https" or parsed.hostname != base.hostname or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment or not parsed.path.startswith("/v1.0/")
                or any(s in (".", "..") for s in unquote(parsed.path).split("/"))
                or "access_token=" in parsed.query.lower()):
            raise ValueError("Microsoft continuation URL left the configured Graph v1.0 origin")
        return url

    def wait(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check()
            time.sleep(min(.5, max(0, until - time.monotonic())))

    def request(self, method, path, **kwargs):
        import requests
        method = method.upper()
        if method != "GET" and (self.side != "target" or not self.writable):
            raise ValueError("this Microsoft connection is read-only")
        url = self.url(path)
        for attempt in range(6):
            self.check()
            try:
                response = self.session.request(method, url, headers={"Authorization": "Bearer " + self.token(),
                                               "Accept": "application/json"}, timeout=(10, 60), allow_redirects=False, **kwargs)
            except requests.RequestException:
                if method != "GET" or attempt == 5:
                    raise GraphError(0, "network_failure; inspect the receipt before retrying a write") from None
                self.wait(retry_delay(None, attempt))
                continue
            if response.status_code in (429, 503, 504) and method == "GET" and attempt < 5:
                delay = retry_delay(response.headers.get("Retry-After"), attempt)
                response.close()
                if delay > 300:
                    raise GraphError(429, "retry_later; Microsoft requested a wait exceeding five minutes")
                self.wait(delay)
                continue
            if not 200 <= response.status_code < 300:
                try:
                    code = response.json().get("error", {}).get("code", "request_failed")
                except (ValueError, AttributeError):
                    code = "request_failed"
                status = response.status_code
                response.close()
                raise GraphError(status, code)
            return response
        raise GraphError(0, "retry_exhausted")

    def get(self, path):
        response = self.request("GET", path)
        try:
            return response.json()
        finally:
            response.close()

    def json_write(self, method, path, value):
        response = self.request(method, path, json=value)
        try:
            return response.json() if response.content else {}
        finally:
            response.close()

    def _storage_url(self, url):
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        suffixes = ("sharepoint.com", "sharepoint.us", "sharepoint-mil.us", "sharepoint.cn", "1drv.com")
        if (parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.fragment or not any(host == s or host.endswith("." + s) for s in suffixes)):
            raise ValueError("Microsoft storage URL is outside the approved storage origins")
        return url

    def download_into(self, path, sink, *, max_bytes):
        """Do not forward Graph bearer tokens to preauthenticated storage URLs."""
        import requests
        self.check()
        response = self.session.get(self.url(path), headers={"Authorization": "Bearer " + self.token()},
                                    allow_redirects=False, stream=True, timeout=(10, 60))
        if response.status_code in (301, 302, 303, 307, 308):
            location = self._storage_url(response.headers.get("Location", ""))
            response.close()
            response = requests.get(location, allow_redirects=False, stream=True, timeout=(10, 60))
        try:
            if response.status_code != 200:
                raise GraphError(response.status_code, "content_download_failed")
            count = 0
            for chunk in response.iter_content(1024 * 1024):
                self.check()
                count += len(chunk)
                if count > max_bytes:
                    raise ValueError("download exceeded the approved size or available staging budget")
                sink.write(chunk)
            return count
        except requests.RequestException:
            raise GraphError(0, "content_download_interrupted") from None
        finally:
            response.close()

    def upload_file(self, path, stream, size):
        import requests
        if self.side != "target" or not self.writable:
            raise ValueError("this Microsoft connection is read-only")
        if size == 0:
            response = self.request("PUT", path + ":/content", data=b"")
            try:
                return response.json()
            finally:
                response.close()
        session = self.json_write("POST", path + ":/createUploadSession",
                                  {"item": {"@microsoft.graph.conflictBehavior": "fail"}})
        url = self._storage_url(session.get("uploadUrl", ""))
        offset = 0
        while offset < size:
            self.check()
            chunk = stream.read(10 * 320 * 1024)
            if not chunk:
                raise ValueError("staged content ended before its recorded size")
            try:
                # Upload URL is already authenticated. It must never receive a Graph token.
                response = requests.put(url, data=chunk, headers={"Content-Length": str(len(chunk)),
                                        "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}"},
                                        allow_redirects=False, timeout=(10, 120))
            except requests.RequestException:
                raise GraphError(0, "upload_interrupted; resume the item after checking its target receipt") from None
            try:
                if response.status_code in (200, 201):
                    if offset + len(chunk) != size:
                        raise GraphError(0, "upload_finished_before_all_bytes")
                    return response.json()
                if response.status_code != 202:
                    raise GraphError(response.status_code, "upload_fragment_failed")
                offset += len(chunk)
            finally:
                response.close()
        raise GraphError(0, "upload_has_no_final_receipt")

    def pages(self, path, max_pages=100000):
        seen = set()
        for _ in range(max_pages):
            url = self.url(path)
            if url in seen:
                raise GraphError(0, "pagination_cycle")
            seen.add(url)
            payload = self.get(url)
            if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
                raise GraphError(0, "malformed_collection")
            yield payload
            path = payload.get("@odata.nextLink")
            if not path:
                return
        raise GraphError(0, "pagination_limit; inventory is incomplete")

    def items(self, path):
        for page in self.pages(path):
            yield from page["value"]

    def close(self):
        self.session.close()
