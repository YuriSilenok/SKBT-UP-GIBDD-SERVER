"""Per-process limits; deploy one worker or add shared limits at the reverse proxy."""
from collections import OrderedDict, deque
from threading import Lock
from time import monotonic
from starlette.responses import JSONResponse


class BodyLimitExceeded(Exception):
    pass


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app
        self.buckets = OrderedDict()
        self.lock = Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        upload = path.endswith("/media/upload")
        maximum = 101 * 1024 ** 2 if upload else 64 * 1024
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = maximum + 1
        if declared < 0 or declared > maximum:
            return await JSONResponse({"detail": "Request body too large"}, 413)(scope, receive, send)
        if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
            host = scope.get("client", ("unknown",))[0]
            category, limit = ("register", 10) if path.endswith("/register") else (
                ("upload", 5) if upload else ("write", 60)
            )
            now = monotonic()
            with self.lock:
                key = (host, category)
                bucket = self.buckets.setdefault(key, deque())
                self.buckets.move_to_end(key)
                while bucket and bucket[0] <= now - 60:
                    bucket.popleft()
                allowed = len(bucket) < limit
                if allowed:
                    bucket.append(now)
                while len(self.buckets) > 10000:
                    self.buckets.popitem(last=False)
            if not allowed:
                return await JSONResponse({"detail": "Too many requests"}, 429,
                    headers={"Retry-After": "60"})(scope, receive, send)
        if upload:
            # Reject unauthenticated/banned uploads before parsing/spooling multipart data.
            from starlette.concurrency import run_in_threadpool
            from fastapi import HTTPException
            def preflight():
                from app.db.database import database_proxy
                from app.services.auth import get_device_by_access_token
                from app.services.messages import _require_sender_can_create_message
                from app.schemas.device import ClientApp
                authorization = headers.get(b"authorization", b"").decode("latin1")
                scheme, _, token = authorization.partition(" ")
                with database_proxy.connection_context():
                    device = get_device_by_access_token(token) if scheme.lower() == "bearer" and token else None
                    if device is None:
                        raise HTTPException(401, "Valid bearer token is required")
                    try:
                        kind = ClientApp(headers.get(b"x-client-app", b"").decode("latin1"))
                    except ValueError:
                        raise HTTPException(422, "Invalid client application")
                    _require_sender_can_create_message(sender=device, client_app=kind)
            try:
                await run_in_threadpool(preflight)
            except HTTPException as exc:
                return await JSONResponse({"detail": exc.detail}, exc.status_code)(scope, receive, send)
        total = 0
        started = False
        async def limited_receive():
            nonlocal total
            event = await receive()
            if event["type"] == "http.request":
                total += len(event.get("body", b""))
                if total > maximum:
                    raise BodyLimitExceeded()
            return event
        async def secured_send(event):
            nonlocal started
            if event["type"] == "http.response.start":
                started = True
                event.setdefault("headers", []).extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                ])
            await send(event)
        try:
            await self.app(scope, limited_receive, secured_send)
        except BodyLimitExceeded:
            if not started:
                await JSONResponse({"detail": "Request body too large"}, 413)(scope, receive, send)
