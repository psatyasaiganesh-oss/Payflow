import hashlib
import json
import threading
import time
import uuid
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from .database import orders, inbox, ledger, audit, control, scenario_runs, refunds
from .providers import MockProvider, ProviderError, StripeTestProvider
from .security import sign, verify


class ServiceError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class PaymentService:
    supported = {"payment_intent.succeeded", "payment_intent.payment_failed",
                 "payment_intent.processing", "payment_intent.canceled",
                 "charge.refunded", "refund.updated", "refund.created", "reconciliation.snapshot"}

    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.provider = MockProvider(db) if settings.provider == "mock" else StripeTestProvider(settings.stripe_secret_key)
        self.scenario_lock = threading.Lock()

    def _audit(self, conn, kind, message, order_id=None):
        conn.execute(audit.insert().values(id=uuid.uuid4().hex, order_id=order_id,
            kind=kind, message=message[:300], created_at=time.time()))

    def get_order(self, order_id):
        with self.db.transaction() as conn:
            row = conn.execute(select(orders).where(orders.c.id == order_id)).mappings().first()
            if not row:
                raise ServiceError(404, "Order not found")
            return dict(row)

    def create_order(self, data, key):
        request_hash, replayed = digest(data), False
        try:
            with self.db.transaction() as conn:
                row = conn.execute(select(orders).where(orders.c.idempotency_key == key)).mappings().first()
                if row:
                    replayed = True
                    if row["request_hash"] != request_hash:
                        raise ServiceError(409, "Idempotency key already belongs to a different request")
                    order = dict(row)
                else:
                    now = time.time()
                    order = dict(id="ord_" + uuid.uuid4().hex[:16], idempotency_key=key,
                        request_hash=request_hash, **data, status="pending", payment_id=None,
                        refunded=0, fulfillment_count=0, last_error=None, created_at=now, updated_at=now)
                    conn.execute(orders.insert().values(**order))
                    self._audit(conn, "order_created", "Order accepted with a persistent idempotency key", order["id"])
        except IntegrityError:
            # A competing PostgreSQL transaction may win the unique-key insert.
            with self.db.transaction() as conn:
                row = conn.execute(select(orders).where(orders.c.idempotency_key == key)).mappings().one()
                if row["request_hash"] != request_hash:
                    raise ServiceError(409, "Idempotency key already belongs to a different request")
                order, replayed = dict(row), True
        if not order["payment_id"]:
            try:
                payment = self.provider.create(order)
            except ProviderError as exc:
                with self.db.transaction() as conn:
                    conn.execute(orders.update().where(orders.c.id == order["id"]).values(last_error=str(exc)[:240]))
                raise ServiceError(502, str(exc)) from exc
            with self.db.transaction() as conn:
                conn.execute(orders.update().where(orders.c.id == order["id"]).values(
                    payment_id=payment["id"], last_error=None, updated_at=time.time()))
        return {"order": self.get_order(order["id"]), "replayed": replayed}

    def receive(self, payload, signature):
        if len(payload) > 1_048_576:
            raise ServiceError(413, "Webhook is too large")
        if not verify(payload, signature, self.settings.webhook_secret):
            raise ServiceError(400, "Invalid or expired webhook signature")
        try:
            event = json.loads(payload)
            event_id, event_type = event["id"], event["type"]
            obj = event["data"]["object"]
            if not isinstance(event_id, str) or not 1 <= len(event_id) <= 100 or not isinstance(event_type, str) or len(event_type) > 100 or not isinstance(obj, dict):
                raise ValueError()
            if event.get("livemode") is not False:
                raise ServiceError(400, "Only explicitly marked test events are accepted")
            payment_id = obj.get("id") if event_type.startswith("payment_intent.") or event_type == "reconciliation.snapshot" else obj.get("payment_intent")
            if event_type in self.supported and (not isinstance(payment_id, str) or not payment_id.startswith("pi_") or len(payment_id) > 80):
                raise ValueError()
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceError(400, "Malformed webhook event") from exc
        event_hash, now = digest(event), time.time()
        try:
            with self.db.transaction() as conn:
                prior = conn.execute(select(inbox).where(inbox.c.id == event_id).with_for_update()).mappings().first()
                if prior:
                    if prior["payload_hash"] != event_hash:
                        raise ServiceError(409, "Event ID was reused with a different payload")
                    conn.execute(inbox.update().where(inbox.c.id == event_id).values(deliveries=inbox.c.deliveries + 1))
                    return {"accepted": True, "duplicate": True, "event_id": event_id}
                ignored = event_type not in self.supported
                conn.execute(inbox.insert().values(id=event_id, payment_id="ignored" if ignored else payment_id,
                    type=event_type, payload=canonical(event), payload_hash=event_hash,
                    status="ignored" if ignored else "pending", deliveries=1, attempts=0,
                    available_at=now, created_at=now, processed_at=now if ignored else None))
        except IntegrityError:
            # Read the committed winner; also count concurrent duplicate deliveries.
            return self.receive(payload, signature)
        # The caller acknowledges only after this transaction has committed.
        return {"accepted": True, "duplicate": False, "event_id": event_id}

    def claim(self, force=False):
        now = time.time()
        with self.db.transaction() as conn:
            flags = conn.execute(select(control).where(control.c.id == 1).with_for_update()).mappings().one()
            if flags["worker_paused"] and not force:
                return None
            conn.execute(inbox.update().where(and_(inbox.c.status == "processing",
                inbox.c.lease_until <= now, inbox.c.attempts >= self.settings.max_attempts)).values(
                status="dead", last_error="Worker lease expired after maximum attempts", lease_token=None))
            eligible = and_(inbox.c.attempts < self.settings.max_attempts,
                or_(and_(inbox.c.status == "pending", inbox.c.available_at <= now),
                    and_(inbox.c.status == "processing", inbox.c.lease_until <= now)))
            row = conn.execute(select(inbox.c.id).where(eligible).order_by(inbox.c.created_at).limit(1)).first()
            if not row:
                return None
            token = uuid.uuid4().hex
            claimed = conn.execute(inbox.update().where(and_(inbox.c.id == row.id, eligible)).values(
                status="processing", lease_token=token, lease_until=now + self.settings.lease_seconds,
                attempts=inbox.c.attempts + 1).returning(inbox)).mappings().first()
            if not claimed:
                return None
            result = dict(claimed)
            result["inject_failure"] = bool(flags["fail_next"])
            if result["inject_failure"]:
                conn.execute(control.update().where(control.c.id == 1).values(fail_next=control.c.fail_next - 1))
            return result

    def _apply(self, conn, order, snapshot):
        if snapshot["amount"] != order["amount"] or snapshot["currency"] != order["currency"]:
            raise ValueError("Provider amount/currency did not match the original order")
        refunded = max(order["refunded"], snapshot.get("refunded", 0))
        if refunded < 0 or refunded > order["amount"]:
            raise ValueError("Provider reported an invalid refunded amount")
        captured = bool(order["fulfillment_count"] or snapshot["status"] == "succeeded")
        if refunded and not captured:
            raise ValueError("Provider reported a refund without a captured payment")
        if captured:
            status = "refunded" if refunded == order["amount"] else "partially_refunded" if refunded else "paid"
        else:
            status = {"failed": "failed", "canceled": "cancelled"}.get(snapshot["status"], "pending")
            if snapshot.get("failed"):
                status = "failed"
        if captured and not order["fulfillment_count"]:
            conn.execute(ledger.insert().values(id=uuid.uuid4().hex, effect_key="capture:" + snapshot["id"],
                order_id=order["id"], kind="capture", amount=order["amount"], currency=order["currency"], created_at=time.time()))
            self._audit(conn, "payment_captured", "Payment verified; order fulfilled once", order["id"])
        delta = refunded - order["refunded"]
        if delta:
            conn.execute(ledger.insert().values(id=uuid.uuid4().hex,
                effect_key=f"refund:{snapshot['id']}:{refunded}", order_id=order["id"],
                kind="refund", amount=-delta, currency=order["currency"], created_at=time.time()))
            self._audit(conn, "refund_recorded", "Refund verified against the provider", order["id"])
        conn.execute(orders.update().where(orders.c.id == order["id"]).values(status=status,
            refunded=refunded, fulfillment_count=int(captured), updated_at=time.time(), last_error=None))

    def process_one(self, force=False):
        job = self.claim(force)
        if not job:
            return None
        try:
            if job["inject_failure"]:
                raise RuntimeError("Injected worker failure")
            snapshot = self.provider.retrieve(job["payment_id"])
            with self.db.transaction() as conn:
                owned = conn.execute(select(inbox).where(and_(inbox.c.id == job["id"],
                    inbox.c.status == "processing", inbox.c.lease_token == job["lease_token"])).with_for_update()).mappings().first()
                if not owned:
                    return {"event_id": job["id"], "status": "lease_lost"}
                order = conn.execute(select(orders).where(orders.c.payment_id == job["payment_id"]).with_for_update()).mappings().first()
                if not order:
                    raise ValueError("Order is not yet associated with this payment")
                self._apply(conn, order, snapshot)
                conn.execute(inbox.update().where(inbox.c.id == job["id"]).values(
                    status="done", processed_at=time.time(), lease_token=None, lease_until=None, last_error=None))
            return {"event_id": job["id"], "status": "done"}
        except Exception as exc:
            message = str(exc)[:240] if isinstance(exc, (ProviderError, ValueError, RuntimeError)) else "Processing failed: " + type(exc).__name__
            dead = job["attempts"] >= self.settings.max_attempts
            with self.db.transaction() as conn:
                conn.execute(inbox.update().where(and_(inbox.c.id == job["id"],
                    inbox.c.lease_token == job["lease_token"])).values(status="dead" if dead else "pending",
                    available_at=time.time() + min(60, 2 ** job["attempts"]), lease_token=None,
                    lease_until=None, last_error=message))
                self._audit(conn, "processing_failure", message)
            return {"event_id": job["id"], "status": "dead" if dead else "retry_scheduled"}

    def drain(self, force=False, limit=100):
        results = []
        for _ in range(limit):
            result = self.process_one(force)
            if result is None:
                break
            results.append(result)
        return results

    def set_control(self, *, paused=None, fail_next=None):
        changes = {}
        if paused is not None:
            changes["worker_paused"] = int(paused)
        if fail_next is not None:
            changes["fail_next"] = fail_next
        with self.db.transaction() as conn:
            conn.execute(control.update().where(control.c.id == 1).values(**changes))

    def retry_event(self, event_id):
        with self.db.transaction() as conn:
            row = conn.execute(select(inbox).where(inbox.c.id == event_id).with_for_update()).mappings().first()
            if not row:
                raise ServiceError(404, "Event not found")
            if row["status"] not in {"dead", "pending"}:
                raise ServiceError(409, "Only pending or dead events can be retried")
            conn.execute(inbox.update().where(inbox.c.id == event_id).values(status="pending",
                attempts=0 if row["status"] == "dead" else row["attempts"], available_at=time.time()))
            self._audit(conn, "manual_retry", "Event scheduled for retry: " + event_id)

    def require_mock(self):
        if self.settings.provider != "mock":
            raise ServiceError(409, "Failure scenarios are available only with the local simulator")

    def make_event(self, payment_id, event_type="payment_intent.succeeded", event_id=None):
        snapshot = self.provider.retrieve(payment_id)
        obj = snapshot if event_type.startswith("payment_intent.") or event_type == "reconciliation.snapshot" else {"id": "ch_demo_" + payment_id, "payment_intent": payment_id}
        return {"id": event_id or "evt_" + uuid.uuid4().hex[:20], "type": event_type,
                "livemode": False, "data": {"object": obj}, "created": int(time.time())}

    def deliver_event(self, event):
        body = canonical(event).encode()
        return self.receive(body, sign(body, self.settings.webhook_secret))

    def emit(self, order_id, outcome="success", repetitions=1):
        self.require_mock()
        order = self.get_order(order_id)
        self.provider.settle(order["payment_id"], "succeeded" if outcome == "success" else "failed")
        event = self.make_event(order["payment_id"], "payment_intent.succeeded" if outcome == "success" else "payment_intent.payment_failed")
        for _ in range(repetitions):
            self.deliver_event(event)
        return event

    def refund_order(self, order_id, amount, key):
        order = self.get_order(order_id)
        if amount <= 0 or amount > order["amount"]:
            raise ServiceError(422, "Refund amount must be positive and no larger than the order")
        try:
            refund_id = self.provider.refund(order, amount, key)
        except ProviderError as exc:
            raise ServiceError(409, str(exc)) from exc
        if self.settings.provider == "mock":
            event = self.make_event(order["payment_id"], "charge.refunded", "evt_" + refund_id)
            with self.db.transaction() as conn:
                record = conn.execute(select(refunds).where(refunds.c.idempotency_key == key)).mappings().one()
            event["created"] = int(record["created_at"])
            self.deliver_event(event)
        return {"refund_id": refund_id, "order": self.get_order(order_id)}

    def reconcile(self):
        with self.db.transaction() as conn:
            candidates = [dict(x) for x in conn.execute(select(orders).where(orders.c.payment_id.is_not(None))).mappings()]
        queued, errors = 0, 0
        for order in candidates:
            try:
                snap = self.provider.retrieve(order["payment_id"])
                captured = snap["status"] == "succeeded"
                drift = (captured and not order["fulfillment_count"]) or snap.get("refunded", 0) > order["refunded"]
                if drift:
                    self.deliver_event(self.make_event(order["payment_id"], "reconciliation.snapshot"))
                    queued += 1
            except ProviderError:
                errors += 1
        self.drain()
        return {"checked": len(candidates), "repair_events_queued": queued, "errors": errors}

    def seed(self):
        self.require_mock()
        products = [("Orbit Workspace", "Anika Rao", 249900, "success"),
                    ("Atlas Cloud", "Rohan Patel", 89900, "success"),
                    ("Design Studio", "Maya Shah", 149900, "success"),
                    ("Developer Pro", "Arjun Kumar", 39900, "decline"),
                    ("Launch Kit", "Sara Das", 129900, None),
                    ("Analytics Plus", "Neha Mehta", 69900, "success")]
        for index, (product, customer, amount, outcome) in enumerate(products):
            result = self.create_order(dict(product=product, customer=customer, amount=amount, currency="inr"), "seed:" + str(index))
            if not result["replayed"] and outcome:
                self.emit(result["order"]["id"], outcome)
        self.drain()
        return {"seeded": True}

    def run_scenario(self, name):
        self.require_mock()
        names = {"duplicate_request", "duplicate_webhook", "out_of_order", "worker_failure", "crash_recovery", "reconciliation", "invalid_signature"}
        if name not in names:
            raise ServiceError(404, "Unknown scenario")
        with self.scenario_lock:
            with self.db.transaction() as conn:
                previous = conn.execute(select(control)).mappings().one()["worker_paused"]
            self.set_control(paused=True)
            try:
                self.drain(force=True)
                data = dict(product="Reliability test", customer="Demo merchant", amount=149900, currency="inr")
                key = "lab:" + uuid.uuid4().hex
                order = self.create_order(data, key)["order"]
                checks = []
                if name == "duplicate_request":
                    results = [self.create_order(data, key) for _ in range(20)]
                    checks.append({"label": "20 repeated requests reuse one order", "passed": all(x["order"]["id"] == order["id"] and x["replayed"] for x in results)})
                    self.emit(order["id"])
                elif name == "invalid_signature":
                    event = self.make_event(order["payment_id"])
                    original = canonical(event).encode()
                    event["data"]["object"]["amount"] = 1
                    try:
                        self.receive(canonical(event).encode(), sign(original, self.settings.webhook_secret))
                        rejected = False
                    except ServiceError as exc:
                        rejected = exc.status == 400
                    checks.append({"label": "Tampered payload rejected before persistence", "passed": rejected})
                elif name == "out_of_order":
                    old = self.make_event(order["payment_id"], "payment_intent.payment_failed")
                    self.emit(order["id"])
                    self.deliver_event(old)
                    checks.append({"label": "Old event delivered after the success event", "passed": True})
                elif name == "reconciliation":
                    self.provider.settle(order["payment_id"])
                    report = self.reconcile()
                    checks.append({"label": "Missing webhook detected and repair queued", "passed": report["repair_events_queued"] >= 1})
                else:
                    event = self.emit(order["id"], repetitions=10 if name == "duplicate_webhook" else 1)
                    if name == "worker_failure":
                        self.set_control(fail_next=1)
                        result = self.process_one(force=True)
                        checks.append({"label": "Worker failure leaves a durable retry", "passed": result["status"] == "retry_scheduled"})
                        self.retry_event(event["id"])
                    elif name == "crash_recovery":
                        claim = self.claim(force=True)
                        with self.db.transaction() as conn:
                            conn.execute(inbox.update().where(inbox.c.id == claim["id"]).values(lease_until=time.time() - 1))
                self.drain(force=True)
                if name in {"duplicate_webhook", "crash_recovery"}:
                    with self.db.transaction() as conn:
                        processed = conn.execute(select(inbox).where(inbox.c.id == event["id"])).mappings().one()
                    if name == "duplicate_webhook":
                        checks.append({"label": "Ten deliveries recorded for one event", "passed": processed["deliveries"] == 10})
                    else:
                        checks.append({"label": "Abandoned claim recovered after lease expiry", "passed": claim["id"] == event["id"] and processed["status"] == "done" and processed["attempts"] >= 2})
                final = self.get_order(order["id"])
                if name != "invalid_signature":
                    checks.append({"label": "Order fulfilled exactly once", "passed": final["fulfillment_count"] == 1 and final["status"] == "paid"})
                    with self.db.transaction() as conn:
                        count = len(conn.execute(select(ledger).where(ledger.c.order_id == order["id"])).all())
                    checks.append({"label": "One capture ledger entry", "passed": count == 1})
                else:
                    checks.append({"label": "No payment or fulfillment side effects", "passed": final["fulfillment_count"] == 0})
                result = dict(id=uuid.uuid4().hex, name=name, passed=all(x["passed"] for x in checks),
                              checks=checks, order_id=order["id"], created_at=time.time())
                with self.db.transaction() as conn:
                    conn.execute(scenario_runs.insert().values(id=result["id"], name=name, passed=int(result["passed"]),
                        details=canonical(result), created_at=result["created_at"]))
                return result
            finally:
                self.set_control(paused=bool(previous), fail_next=0)

    def state(self):
        with self.db.transaction() as conn:
            all_orders = [dict(x) for x in conn.execute(select(orders).order_by(orders.c.created_at.desc())).mappings()]
            events = [dict(x) for x in conn.execute(select(inbox).order_by(inbox.c.created_at.desc())).mappings()]
            entries = [dict(x) for x in conn.execute(select(ledger).order_by(ledger.c.created_at.desc()).limit(100)).mappings()]
            activity = [dict(x) for x in conn.execute(select(audit).order_by(audit.c.created_at.desc()).limit(50)).mappings()]
            runs = [json.loads(x["details"]) for x in conn.execute(select(scenario_runs).order_by(scenario_runs.c.created_at.desc()).limit(20)).mappings()]
            flags = dict(conn.execute(select(control)).mappings().one())
        currencies = {}
        for order in all_orders:
            current = currencies.setdefault(order["currency"], {"captured": 0, "refunded": 0, "net": 0})
            if order["fulfillment_count"]:
                current["captured"] += order["amount"]
                current["refunded"] += order["refunded"]
                current["net"] += order["amount"] - order["refunded"]
        backlog = [x for x in events if x["status"] in {"pending", "processing"}]
        decided = [x for x in all_orders if x["fulfillment_count"] or x["status"] == "failed"]
        stats = {"orders": len(all_orders), "captured_orders": sum(x["fulfillment_count"] for x in all_orders),
            "failed_orders": sum(x["status"] == "failed" for x in all_orders), "by_currency": currencies,
            "success_rate": round(100 * sum(x["fulfillment_count"] for x in decided) / len(decided), 1) if decided else None,
            "backlog": len(backlog), "dead_events": sum(x["status"] == "dead" for x in events),
            "events_processed": sum(x["status"] == "done" for x in events),
            "duplicate_deliveries": sum(x["deliveries"] - 1 for x in events),
            "oldest_pending_seconds": round(max([time.time() - x["created_at"] for x in backlog], default=0), 1)}
        for item in events:
            item.pop("payload", None)
            item.pop("payload_hash", None)
            item.pop("lease_token", None)
        return dict(provider=self.provider.name, worker_paused=bool(flags["worker_paused"]),
            orders=all_orders[:100], events=events[:100], ledger=entries, activity=activity,
            runs=runs, stats=stats, updated_at=time.time())
