import asyncio
import hmac
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from .config import Settings
from .database import Database, control
from .service import PaymentService, ServiceError

WEB = Path(__file__).resolve().parents[1] / "web"


class OrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product: str = Field(min_length=1, max_length=100)
    customer: str = Field(min_length=1, max_length=120)
    amount: int = Field(ge=50, le=100_000_000, strict=True)
    currency: Literal["inr", "usd"] = "inr"


class RefundInput(BaseModel):
    amount: int = Field(gt=0, le=100_000_000, strict=True)


class EmitInput(BaseModel):
    outcome: Literal["success", "decline"] = "success"
    repetitions: int = Field(default=1, ge=1, le=50)


def create_app(settings=None):
    settings = settings or Settings.from_env()
    settings.validate()
    db = Database(settings.database_url)
    service = PaymentService(db, settings)

    async def worker_loop():
        while True:
            try:
                await asyncio.to_thread(service.drain, False, 20)
            except Exception:
                # A temporary database outage must not terminate the worker loop.
                pass
            await asyncio.sleep(0.75)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker_loop()) if settings.auto_worker else None
        yield
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        db.close()

    app = FastAPI(title="PayFlow", version="1.0.0", lifespan=lifespan,
        description="A test-payment reliability lab: durable webhooks, recovery, reconciliation and monitoring.")
    app.state.service, app.state.db = service, db
    app.state.latencies = deque(maxlen=500)

    def authorize(authorization: str = Header(default="")):
        if settings.api_token and not hmac.compare_digest(authorization.encode(), ("Bearer " + settings.api_token).encode()):
            raise HTTPException(status_code=401, detail="Enter your PayFlow API access token")

    def operation_key(idempotency_key: str = Header(default="", alias="Idempotency-Key")):
        if not 1 <= len(idempotency_key) <= 200 or not idempotency_key.isascii() or any(ord(x) < 33 for x in idempotency_key):
            raise HTTPException(status_code=422, detail="Supply an ASCII Idempotency-Key header of 1–200 characters")
        return idempotency_key

    @app.exception_handler(ServiceError)
    async def service_error(_, exc):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.middleware("http")
    async def request_timing(request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - start) * 1000
        if request.url.path.startswith("/api/"):
            app.state.latencies.append(elapsed)
        response.headers["X-Request-ID"] = uuid.uuid4().hex
        response.headers["Server-Timing"] = f"app;dur={elapsed:.2f}"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health/live")
    def live():
        return {"status": "alive", "service": "payflow"}

    @app.get("/health/ready")
    def ready():
        try:
            with db.transaction() as conn:
                flags = conn.execute(select(control)).mappings().one()
            return {"status": "ready", "database": "connected", "worker_paused": bool(flags["worker_paused"])}
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not_ready", "database": "unavailable"})

    @app.get("/api/state", dependencies=[Depends(authorize)])
    def state():
        value = service.state()
        timings = sorted(app.state.latencies)
        value["monitoring"] = {"api_samples": len(timings),
            "api_p95_ms": round(timings[min(len(timings)-1, int(len(timings)*0.95))], 2) if timings else None,
            "database": "connected", "automatic_worker": settings.auto_worker}
        return value

    @app.post("/api/orders", dependencies=[Depends(authorize)])
    def new_order(data: OrderInput, key=Depends(operation_key)):
        return service.create_order(data.model_dump(), key)

    @app.post("/api/orders/{order_id}/refund", dependencies=[Depends(authorize)])
    def refund(order_id: str, data: RefundInput, key=Depends(operation_key)):
        return service.refund_order(order_id, data.amount, key)

    @app.post("/webhooks/stripe")
    async def webhook(request: Request, stripe_signature: str = Header(default="")):
        # Stream with a bound before allocating a potentially huge request body.
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > 1_048_576:
                raise HTTPException(413, "Webhook is too large")
        return service.receive(bytes(payload), stripe_signature)

    @app.post("/api/reconcile", dependencies=[Depends(authorize)])
    def reconcile():
        return service.reconcile()

    @app.post("/api/worker/{action}", dependencies=[Depends(authorize)])
    def worker_action(action: Literal["pause", "resume", "run", "fail-next"]):
        if action in {"pause", "resume", "fail-next"}:
            service.require_mock()
        if action == "pause":
            service.set_control(paused=True)
        elif action == "resume":
            service.set_control(paused=False)
        elif action == "fail-next":
            service.set_control(fail_next=1)
        return {"processed": service.drain() if action in {"resume", "run"} else [], "action": action}

    @app.post("/api/events/{event_id}/retry", dependencies=[Depends(authorize)])
    def retry(event_id: str):
        service.retry_event(event_id)
        return {"scheduled": True}

    @app.post("/api/demo/seed", dependencies=[Depends(authorize)])
    def seed():
        return service.seed()

    @app.post("/api/demo/scenarios/{name}", dependencies=[Depends(authorize)])
    def scenario(name: str):
        return service.run_scenario(name)

    @app.post("/api/demo/orders/{order_id}/emit", dependencies=[Depends(authorize)])
    def emit(order_id: str, data: EmitInput):
        event = service.emit(order_id, data.outcome, data.repetitions)
        service.drain()
        return {"event_id": event["id"], "order": service.get_order(order_id)}

    @app.get("/metrics", dependencies=[Depends(authorize)], response_class=PlainTextResponse)
    def metrics():
        stats = service.state()["stats"]
        lines = ["# HELP payflow_backlog Events awaiting processing.", "# TYPE payflow_backlog gauge"]
        for key in ["orders", "captured_orders", "failed_orders", "backlog", "dead_events", "events_processed", "duplicate_deliveries", "oldest_pending_seconds"]:
            lines.append(f"payflow_{key} {stats[key]}")
        return "\n".join(lines) + "\n"

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(WEB / "index.html")

    app.mount("/assets", StaticFiles(directory=WEB), name="assets")
    return app
