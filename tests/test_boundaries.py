import time
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select
from payflow.app import create_app
from payflow.config import Settings
from payflow.database import inbox
from payflow.providers import ProviderError, StripeTestProvider
from payflow.security import sign, verify
from payflow.service import canonical


def test_api_token_and_webhook_have_separate_authentication(database_url):
    app = create_app(Settings(database_url=database_url, api_token="test-access-token", auto_worker=False))
    with TestClient(app) as client:
        assert client.get("/api/state").status_code == 401
        assert client.get("/metrics").status_code == 401
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/state", headers={"Authorization":"Bearer test-access-token"}).status_code == 200
        event = {"id":"evt_ignored", "type":"customer.created", "livemode":False, "data":{"object":{"id":"cus_test"}}}
        body = canonical(event).encode()
        assert client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature":sign(body, app.state.service.settings.webhook_secret)}).status_code == 200


def test_unknown_events_ignore_untrusted_payment_reference(client, service):
    event = {"id":"evt_unknown", "type":"unhandled.event", "livemode":False, "data":{"object":{"payment_intent":{"expanded":"object"}}}}
    body = canonical(event).encode()
    response = client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature":sign(body, service.settings.webhook_secret)})
    assert response.status_code == 200
    with service.db.transaction() as conn:
        assert conn.execute(select(inbox.c.status)).scalar_one() == "ignored"


def test_reused_event_id_with_different_payload_is_conflict(client, service, order_data):
    order = service.create_order(order_data, "event-collision")["order"]
    event = service.make_event(order["payment_id"])
    service.deliver_event(event)
    event["type"] = "payment_intent.processing"
    body = canonical(event).encode()
    assert client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature":sign(body, service.settings.webhook_secret)}).status_code == 409


def test_oversized_webhook_has_no_side_effects(client, service):
    body = b"x" * 1_048_577
    assert client.post("/webhooks/stripe", content=body).status_code == 413
    assert service.state()["events"] == []


def test_automatic_worker_processes_durable_webhook(database_url, order_data):
    app = create_app(Settings(database_url=database_url, auto_worker=True))
    with TestClient(app) as client:
        service = app.state.service
        order = service.create_order(order_data, "automatic-worker")["order"]
        service.provider.settle(order["payment_id"])
        body = canonical(service.make_event(order["payment_id"])).encode()
        response = client.post("/webhooks/stripe", content=body,
            headers={"Stripe-Signature":sign(body, service.settings.webhook_secret)})
        assert response.status_code == 200
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not service.get_order(order["id"])["fulfillment_count"]:
            time.sleep(0.05)
        assert service.get_order(order["id"])["fulfillment_count"] == 1


def test_rotated_signature_accepts_matching_v1_only():
    body = b'{"test":true}'
    good = sign(body, "secret")
    assert verify(body, good + ",v1=" + "0" * 64, "secret")
    assert not verify(body, good + ",t=" + str(int(time.time())), "secret")
    assert not verify(body, "t=" + str(int(time.time())) + ",v1=é", "secret")


@pytest.mark.parametrize("settings", [
    Settings(provider="other"),
    Settings(provider="stripe_test", stripe_secret_key="sk_live_invalid"),
    Settings(provider="stripe_test", stripe_secret_key="sk_test_fixture"),
    Settings(provider="stripe_test", stripe_secret_key="sk_test_fixture", webhook_secret="whsec_fixture"),
])
def test_stripe_configuration_requires_explicit_test_setup(settings):
    with pytest.raises(ValueError):
        settings.validate()


def test_stripe_adapter_sends_stable_provider_keys(monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, json={"id":"re_test" if url.endswith("refunds") else "pi_test", "livemode":False})
    monkeypatch.setattr(httpx, "request", request)
    provider = StripeTestProvider("sk_test_fixture")
    order = {"id":"ord_test", "payment_id":"pi_test", "amount":149900, "currency":"inr", "product":"Developer Pro"}
    provider.create(order)
    provider.create(order)
    provider.refund(order, 49900, "refund-one")
    assert calls[0][2]["headers"]["Idempotency-Key"] == calls[1][2]["headers"]["Idempotency-Key"] == "create:ord_test"
    assert calls[0][2]["data"]["amount"] == "149900"
    assert calls[0][2]["data"]["payment_method"] == "pm_card_visa"
    assert calls[2][2]["headers"]["Idempotency-Key"] == "refund:ord_test:refund-one"


def test_stripe_adapter_reads_cumulative_refund_truth(monkeypatch):
    monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: httpx.Response(200, json={
        "id":"pi_test", "amount":149900, "currency":"inr", "status":"succeeded", "livemode":False,
        "latest_charge":{"amount_refunded":49900}}))
    assert StripeTestProvider("sk_test_fixture").retrieve("pi_test")["refunded"] == 49900


def test_stripe_adapter_does_not_expose_remote_error_message(monkeypatch):
    monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: httpx.Response(400, json={
        "error":{"code":"card_declined", "message":"secret provider response"}}))
    with pytest.raises(ProviderError, match="card_declined") as exc:
        StripeTestProvider("sk_test_fixture")._request("GET", "test")
    assert "secret provider response" not in str(exc.value)


def test_stripe_adapter_rejects_live_response(monkeypatch):
    monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: httpx.Response(200, json={"livemode":True}))
    with pytest.raises(ProviderError, match="Live"):
        StripeTestProvider("sk_test_fixture")._request("GET", "test")
