import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx

ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[None]]
logger = logging.getLogger("api_sentinel")
TRACEPARENT = re.compile(rb"^[\da-f]{2}-([\da-f]{32})-[\da-f]{16}-[\da-f]{2}$", re.IGNORECASE)
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

class SentinelMiddleware:
    """Capture request metadata and deliver it asynchronously to API Sentinel."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        service_key: str,
        collector_url: str = "http://127.0.0.1:8000/api/telemetry/batch",
        region: str | None = None,
        queue_size: int = 1000,
        batch_size: int = 50,
        flush_interval: float = 1.0,
        timeout: float = 3.0,
        ignore_paths: tuple[str, ...] = ("/api/telemetry",),
    ):
        if not service_key.startswith("as_live_"):
            raise ValueError("service_key must be an API Sentinel ingestion key")
        if not 1 <= batch_size <= 100:
            raise ValueError("batch_size must be between 1 and 100")
        self.app = app
        self.service_key = service_key
        self.collector_url = collector_url
        self.region = region
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.timeout = timeout
        self.ignore_paths = tuple(path.rstrip("/") or "/" for path in ignore_paths)
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self.client: httpx.AsyncClient | None = None
        self.worker: asyncio.Task[None] | None = None
        self.start_lock = asyncio.Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            async def lifespan_send(message):
                if message["type"] == "lifespan.startup.complete":
                    await self.start()
                elif message["type"] == "lifespan.shutdown.complete":
                    await self.close()
                await send(message)

            await self.app(scope, receive, lifespan_send)
            return

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        if any(path == prefix or path.startswith(prefix + "/") for prefix in self.ignore_paths):
            await self.app(scope, receive, send)
            return

        await self.start()
        started = perf_counter()
        status_code = 500
        response_size = None
        error_type = None
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        request_size = _int_header(headers.get(b"content-length"))

        async def capture_send(message):
            nonlocal status_code, response_size
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers = {key.lower(): value for key, value in message.get("headers", [])}
                response_size = _int_header(response_headers.get(b"content-length"))
            await send(message)

        try:
            await self.app(scope, receive, capture_send)
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            route = scope.get("route")
            endpoint = getattr(route, "path", "<unmatched>")
            request_id = _safe_request_id(headers.get(b"x-request-id"))
            trace_id = _trace_id(headers.get(b"traceparent"))
            event = {
                "event_id": str(uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "endpoint": endpoint[:300],
                "method": scope.get("method", "GET")[:12].upper(),
                "status_code": status_code,
                "latency_ms": round((perf_counter() - started) * 1000, 3),
                "request_size": request_size,
                "response_size": response_size,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_type": error_type,
                "region": self.region,
            }
            try:
                self.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Telemetry queue is full; dropping an event")

    async def start(self):
        async with self.start_lock:
            if self.worker and not self.worker.done():
                return
            self.client = httpx.AsyncClient(timeout=self.timeout)
            self.worker = asyncio.create_task(self._run_worker(), name="api-sentinel-telemetry")

    async def close(self):
        if not self.worker:
            return
        try:
            await asyncio.wait_for(self.queue.join(), timeout=max(5.0, self.timeout * 4))
        except TimeoutError:
            logger.warning("Timed out flushing telemetry during shutdown")
        self.worker.cancel()
        try:
            await self.worker
        except asyncio.CancelledError:
            pass
        if self.client:
            await self.client.aclose()
            self.client = None
        self.worker = None

    async def _run_worker(self):
        while True:
            try:
                first = await asyncio.wait_for(self.queue.get(), timeout=self.flush_interval)
            except TimeoutError:
                continue
            batch = [first]
            deadline = asyncio.get_running_loop().time() + self.flush_interval
            while len(batch) < self.batch_size:
                try:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    next_event = await asyncio.wait_for(self.queue.get(), timeout=remaining)
                    batch.append(next_event)
                except TimeoutError:
                    break
            await self._send_batch(batch)
            for _ in batch:
                self.queue.task_done()

    async def _send_batch(self, events):
        if not self.client:
            return
        for attempt in range(3):
            try:
                response = await self.client.post(
                    self.collector_url,
                    headers={"X-Sentinel-Key": self.service_key},
                    json={"events": events},
                )
                response.raise_for_status()
                return
            except (httpx.HTTPError, ValueError):
                if attempt < 2:
                    await asyncio.sleep(0.25 * (2**attempt))
        logger.warning("Could not deliver telemetry batch after retries")


def _int_header(value: bytes | None) -> int | None:
    if value is None:
        return None
    try:
        size = int(value)
        return size if size >= 0 else None
    except ValueError:
        return None


def _safe_request_id(value: bytes | None) -> str | None:
    if not value:
        return None
    try:
        request_id = value.decode("ascii")
    except UnicodeDecodeError:
        return None
    return request_id if SAFE_REQUEST_ID.fullmatch(request_id) else None


def _trace_id(value: bytes | None) -> str | None:
    if not value:
        return None
    match = TRACEPARENT.fullmatch(value)
    if not match:
        return None
    trace_id = match.group(1).decode("ascii").lower()
    return trace_id if trace_id != "0" * 32 else None
