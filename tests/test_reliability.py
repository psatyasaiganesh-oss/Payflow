import json
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select
from payflow.database import inbox, ledger, payments
from payflow.security import sign
from payflow.service import canonical


def new_order(client, data, key="checkout-1"):
    result = client.post("/api/orders", json=data, headers={"Idempotency-Key": key})
    assert result.status_code == 200, result.text
    return result.json()["order"]


def test_idempotency_persists_across_service_restart(client, service, order_data):
    from payflow.database import Database
    from payflow.service import PaymentService
    order = new_order(client, order_data)
    db = Database(service.settings.database_url)
    try:
        restarted = PaymentService(db, service.settings)
        repeated = restarted.create_order(order_data, "checkout-1")
        assert repeated["replayed"] and repeated["order"]["id"] == order["id"]
    finally:
        db.close()
    with service.db.transaction() as conn:
        assert len(conn.execute(select(payments)).all()) == 1


def test_conflicting_idempotency_key(client, order_data):
    new_order(client, order_data)
    changed = {**order_data, "amount": 299900}
    assert client.post("/api/orders", json=changed, headers={"Idempotency-Key": "checkout-1"}).status_code == 409


def test_concurrent_order_creation(service, order_data):
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: service.create_order(order_data, "parallel"), range(20)))
    assert len({x["order"]["id"] for x in rows}) == 1
    with service.db.transaction() as conn:
        assert len(conn.execute(select(payments)).all()) == 1


@pytest.mark.parametrize("scenario", ["duplicate_request", "duplicate_webhook", "out_of_order",
    "worker_failure", "crash_recovery", "reconciliation", "invalid_signature"])
def test_demo_scenarios_execute_real_service(client, scenario):
    result = client.post("/api/demo/scenarios/" + scenario)
    assert result.status_code == 200, result.text
    assert result.json()["passed"], result.json()


def test_webhook_is_durable_before_acknowledgment(client, service, order_data):
    order = new_order(client, order_data)
    service.provider.settle(order["payment_id"])
    body = canonical(service.make_event(order["payment_id"])).encode()
    response = client.post("/webhooks/stripe", content=body,
        headers={"Stripe-Signature": sign(body, service.settings.webhook_secret)})
    assert response.status_code == 200
    with service.db.transaction() as conn:
        row = conn.execute(select(inbox)).mappings().one()
        assert row["status"] == "pending" and row["attempts"] == 0
    assert service.get_order(order["id"])["fulfillment_count"] == 0
    service.drain()
    assert service.get_order(order["id"])["fulfillment_count"] == 1


def test_duplicate_events_with_distinct_ids_do_not_duplicate_fulfillment(service, order_data):
    order = service.create_order(order_data, "distinct")["order"]
    service.provider.settle(order["payment_id"])
    for _ in range(10):
        service.deliver_event(service.make_event(order["payment_id"]))
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: service.drain(), range(6)))
    assert service.get_order(order["id"])["fulfillment_count"] == 1
    with service.db.transaction() as conn:
        assert len(conn.execute(select(ledger)).all()) == 1
        assert all(x["status"] == "done" for x in conn.execute(select(inbox)).mappings())


@pytest.mark.parametrize("header_kind", ["missing", "expired", "future", "wrong_secret"])
def test_invalid_signature_has_no_side_effects(client, service, order_data, header_kind):
    order = new_order(client, order_data)
    body = canonical(service.make_event(order["payment_id"])).encode()
    headers = {"missing": "", "expired": sign(body, service.settings.webhook_secret, int(time.time()) - 1000),
        "future": sign(body, service.settings.webhook_secret, int(time.time()) + 1000),
        "wrong_secret": sign(body, "incorrect")}
    assert client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": headers[header_kind]}).status_code == 400
    assert service.state()["events"] == []


def test_live_event_rejected(client, service, order_data):
    order = new_order(client, order_data)
    event = service.make_event(order["payment_id"])
    event["livemode"] = True
    body = canonical(event).encode()
    assert client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": sign(body, service.settings.webhook_secret)}).status_code == 400


def test_refund_idempotency_and_partial_refund(client, service, order_data):
    order = new_order(client, order_data)
    service.emit(order["id"])
    service.drain()
    for _ in range(5):
        response = client.post(f"/api/orders/{order['id']}/refund", json={"amount": 49900}, headers={"Idempotency-Key": "refund-1"})
        assert response.status_code == 200, response.text
    service.drain()
    current = service.get_order(order["id"])
    assert current["refunded"] == 49900 and current["status"] == "partially_refunded"
    with service.db.transaction() as conn:
        assert sum(x["amount"] for x in conn.execute(select(ledger)).mappings()) == 100000


def test_refund_cannot_exceed_balance(client, service, order_data):
    order = new_order(client, order_data)
    service.emit(order["id"])
    service.drain()
    assert client.post(f"/api/orders/{order['id']}/refund", json={"amount": 149900}, headers={"Idempotency-Key": "full"}).status_code == 200
    assert client.post(f"/api/orders/{order['id']}/refund", json={"amount": 50}, headers={"Idempotency-Key": "again"}).status_code == 409
    service.drain()
    assert service.get_order(order["id"])["status"] == "refunded"


def test_concurrent_refund_replays_change_provider_balance_once(service, order_data):
    order = service.create_order(order_data, "concurrent-refund")["order"]
    service.emit(order["id"])
    service.drain()
    with ThreadPoolExecutor(max_workers=8) as pool:
        # Test the provider boundary: each retry must return the same refund.
        ids = list(pool.map(lambda _: service.provider.refund(order, 49900, "same-refund"), range(20)))
    assert len(set(ids)) == 1
    assert service.provider.retrieve(order["payment_id"])["refunded"] == 49900


def test_refund_replay_after_another_refund_remains_stable(service, order_data):
    order = service.create_order(order_data, "refund-history")["order"]
    service.emit(order["id"])
    service.drain()
    first = service.refund_order(order["id"], 49900, "refund-first")
    service.drain()
    service.refund_order(order["id"], 20000, "refund-second")
    service.drain()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.refund_order(order["id"], 49900, "refund-first"), range(20)))
    assert {result["refund_id"] for result in results} == {first["refund_id"]}
    service.drain()
    assert service.get_order(order["id"])["refunded"] == 69900
    with service.db.transaction() as conn:
        assert sum(row.amount for row in conn.execute(select(ledger.c.amount))) == 80000


def test_concurrent_overlapping_refunds_cannot_overdraw(service, order_data):
    from payflow.providers import ProviderError
    order = service.create_order(order_data, "overlap")["order"]
    service.emit(order["id"])
    def refund(key):
        try:
            return service.provider.refund(order, 100000, key)
        except ProviderError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(refund, ["one", "two"]))
    assert sum(result is not None for result in results) == 1
    assert service.provider.retrieve(order["payment_id"])["refunded"] == 100000


def test_failures_dead_letter_then_manual_recovery(service, order_data):
    order = service.create_order(order_data, "dead-letter")["order"]
    event = service.emit(order["id"])
    for attempt in range(service.settings.max_attempts):
        service.set_control(fail_next=1)
        with service.db.transaction() as conn:
            conn.execute(inbox.update().where(inbox.c.id == event["id"]).values(available_at=0))
        result = service.process_one()
    assert result["status"] == "dead"
    assert service.get_order(order["id"])["fulfillment_count"] == 0
    service.retry_event(event["id"])
    service.drain()
    assert service.get_order(order["id"])["fulfillment_count"] == 1


def test_currency_totals_are_separate(client, service, order_data):
    for currency in ["inr", "usd"]:
        order = new_order(client, {**order_data, "currency": currency}, currency)
        service.emit(order["id"])
    service.drain()
    totals = service.state()["stats"]["by_currency"]
    assert totals["inr"]["net"] == 149900 and totals["usd"]["net"] == 149900


@pytest.mark.parametrize("amount", [0, -1, 1.25, True, "149900"])
def test_invalid_money_rejected(client, order_data, amount):
    assert client.post("/api/orders", json={**order_data, "amount": amount}, headers={"Idempotency-Key": "invalid"}).status_code == 422


def test_health_dashboard_metrics_and_seed(client):
    assert client.get("/").status_code == 200
    assert client.get("/health/ready").json()["database"] == "connected"
    assert client.post("/api/demo/seed").status_code == 200
    assert client.post("/api/demo/seed").status_code == 200
    assert client.get("/api/state").json()["stats"]["orders"] == 6
    assert "payflow_orders 6" in client.get("/metrics").text
