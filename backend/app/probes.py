"""Read-only API probes with DNS validation and address-pinned sockets."""
import asyncio
import http.client
import ipaddress
import json
import os
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit


MAX_SPEC_BYTES = 1_000_000
MAX_ENDPOINTS = 8
REQUEST_TIMEOUT_SECONDS = 5


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str):
        super().__init__(host, port, timeout=REQUEST_TIMEOUT_SECONDS)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout, self.source_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str):
        super().__init__(host, port, timeout=REQUEST_TIMEOUT_SECONDS, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw_socket = socket.create_connection((self.address, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("URL contains an invalid port") from error
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port or (443 if parsed.scheme == "https" else 80)


def _validated_target(url: str) -> tuple[str, list[str]]:
    url = url.strip()
    if len(url) > 2000:
        raise ValueError("URL is too long")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a full http:// or https:// URL")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("URL contains an invalid port") from error
    if port == 0:
        raise ValueError("URL port must be between 1 and 65535")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("URLs cannot contain credentials, query parameters, or fragments")
    if any(ord(char) < 32 for char in url):
        raise ValueError("URL contains invalid characters")

    allow_private = os.getenv("SENTINEL_ALLOW_PRIVATE_API_TARGETS", "false").lower() == "true"
    if parsed.scheme != "https" and not allow_private:
        raise ValueError("Public API targets must use HTTPS")
    host = parsed.hostname.encode("idna").decode("ascii")
    port = port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = list(dict.fromkeys(
            item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ))
    except socket.gaierror as error:
        raise ValueError("The API hostname could not be resolved") from error
    if not addresses:
        raise ValueError("The API hostname has no usable address")
    if not allow_private:
        for address in addresses:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if not ip.is_global:
                raise ValueError("This target resolves to a private or reserved address")
    normalized = url.rstrip("/") or f"{parsed.scheme}://{parsed.netloc}"
    return normalized, addresses


async def validate_api_url(url: str) -> str:
    normalized, _ = await asyncio.to_thread(_validated_target, url)
    return normalized


def _request_sync(url: str, headers: dict[str, str], *, read_body: bool = False):
    started = time.perf_counter()
    normalized, addresses = _validated_target(url)
    parsed = urlsplit(normalized)
    host = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target = parsed.path or "/"
    last_error = None
    for address in addresses:
        connection = (
            _PinnedHTTPSConnection(host, port, address)
            if parsed.scheme == "https"
            else _PinnedHTTPConnection(host, port, address)
        )
        try:
            connection.request("GET", target, headers={**headers, "Accept-Encoding": "identity"})
            response = connection.getresponse()
            body = response.read(MAX_SPEC_BYTES + 1) if read_body else b""
            if read_body and len(body) > MAX_SPEC_BYTES:
                raise ValueError("OpenAPI document exceeds the 1 MB limit")
            return response.status, (time.perf_counter() - started) * 1000, body, None
        except ValueError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            last_error = error
        finally:
            connection.close()
    error_type = type(last_error).__name__ if last_error else "ConnectionError"
    return None, (time.perf_counter() - started) * 1000, b"", error_type


async def _request(url: str, headers: dict[str, str], *, read_body: bool = False):
    return await asyncio.to_thread(_request_sync, url, headers, read_body=read_body)


def _safe_check(url: str, status_code: int | None, latency_ms: float | None, error_type: str | None) -> dict:
    return {"url": url, "method": "GET", "status_code": status_code,
            "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
            "error_type": error_type}


def _discovered_gets(spec: dict, base_url: str) -> list[str]:
    servers = spec.get("servers") or []
    server_url = base_url
    if servers and isinstance(servers[0], dict) and isinstance(servers[0].get("url"), str):
        declared = urljoin(base_url.rstrip("/") + "/", servers[0]["url"])
        try:
            if _origin(declared) == _origin(base_url) and not urlsplit(declared).query and not urlsplit(declared).fragment and not urlsplit(declared).username:
                server_url = declared
        except ValueError:
            pass
    endpoints = []
    for path, operations in (spec.get("paths") or {}).items():
        if not isinstance(path, str) or "{" in path or "}" in path or not isinstance(operations, dict):
            continue
        operation = operations.get("get")
        if not isinstance(operation, dict):
            continue
        parameters = [*(operations.get("parameters") or []), *(operation.get("parameters") or [])]
        if any(isinstance(item, dict) and item.get("in") == "query" and item.get("required") for item in parameters):
            continue
        target = urljoin(server_url.rstrip("/") + "/", path.lstrip("/"))
        try:
            if _origin(target) == _origin(base_url) and not urlsplit(target).query and not urlsplit(target).fragment and not urlsplit(target).username:
                endpoints.append(target)
        except ValueError:
            continue
        if len(endpoints) >= MAX_ENDPOINTS:
            break
    return list(dict.fromkeys(endpoints))


def _result_status(results: list[dict]) -> str:
    if not results:
        return "unreachable"
    healthy = sum(200 <= (item["status_code"] or 0) < 400 for item in results)
    has_response = any(item["status_code"] is not None for item in results)
    return "healthy" if healthy == len(results) else "degraded" if has_response else "unreachable"


async def analyze_api(base_url: str, openapi_url: str | None, auth_header: str | None, auth_value: str | None) -> dict:
    base = await validate_api_url(base_url)
    spec_url = await validate_api_url(openapi_url) if openapi_url else None
    if spec_url and _origin(spec_url) != _origin(base):
        raise ValueError("The OpenAPI document must use the same API host")
    headers = {auth_header: auth_value} if auth_header and auth_value else {}
    endpoints = [base]
    note = None

    if spec_url:
        code, _, body, error_type = await _request(spec_url, headers, read_body=True)
        if code == 200 and not error_type:
            try:
                spec = json.loads(body)
                if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
                    raise ValueError("OpenAPI JSON must include a paths object")
                discovered = _discovered_gets(spec, base)
                validated = []
                for endpoint in discovered:
                    try:
                        target = await validate_api_url(endpoint)
                        if _origin(target) == _origin(base):
                            validated.append(target)
                    except ValueError:
                        continue
                endpoints = validated or [base]
            except (json.JSONDecodeError, UnicodeDecodeError):
                note = "Could not read the OpenAPI document as JSON; checking the supplied URL only."
            except ValueError as error:
                note = str(error)
        else:
            reason = error_type or f"HTTP {code}"
            note = f"OpenAPI document check failed ({reason}); checking the supplied URL only."

    checks = await asyncio.gather(*(_request(endpoint, headers) for endpoint in endpoints))
    results = [_safe_check(endpoint, code, latency, error_type)
               for endpoint, (code, latency, _, error_type) in zip(endpoints, checks)]
    return {"base_url": base, "openapi_url": spec_url, "endpoints": endpoints,
            "checks": results, "status": _result_status(results), "note": note,
            "checked_at": datetime.now(timezone.utc).isoformat()}


async def recheck_endpoints(endpoints: list[str], auth_header: str | None, auth_value: str | None) -> tuple[list[dict], str]:
    headers = {auth_header: auth_value} if auth_header and auth_value else {}
    safe_endpoints = [await validate_api_url(endpoint) for endpoint in endpoints[:MAX_ENDPOINTS]]
    checks = await asyncio.gather(*(_request(endpoint, headers) for endpoint in safe_endpoints))
    results = [_safe_check(endpoint, code, latency, error_type)
               for endpoint, (code, latency, _, error_type) in zip(safe_endpoints, checks)]
    return results, _result_status(results)
