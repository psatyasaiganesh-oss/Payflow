import uuid
import time
import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .database import orders, payments, refunds


class ProviderError(Exception):
    pass


class MockProvider:
    """An independent, persistent source of truth for deterministic local tests."""
    name = "mock"

    def __init__(self, db):
        self.db = db

    def create(self, order):
        with self.db.transaction() as conn:
            # Serialize simulator creation for this order on PostgreSQL too.
            conn.execute(select(orders.c.id).where(orders.c.id == order["id"]).with_for_update()).one()
            row = conn.execute(select(payments).where(payments.c.order_id == order["id"])).mappings().first()
            if row:
                return dict(row)
            data = dict(id="pi_demo_" + uuid.uuid4().hex[:20], order_id=order["id"],
                        amount=order["amount"], currency=order["currency"],
                        status="requires_payment_method", refunded=0)
            conn.execute(payments.insert().values(**data))
            return data

    def retrieve(self, payment_id):
        with self.db.transaction() as conn:
            row = conn.execute(select(payments).where(payments.c.id == payment_id)).mappings().first()
            if not row:
                raise ProviderError("Payment not found at simulator")
            return dict(row)

    def settle(self, payment_id, outcome="succeeded"):
        with self.db.transaction() as conn:
            row = conn.execute(select(payments).where(payments.c.id == payment_id).with_for_update()).mappings().one()
            if row["status"] == "succeeded" and outcome != "succeeded":
                raise ProviderError("A captured payment cannot regress to a failed payment")
            conn.execute(payments.update().where(payments.c.id == payment_id).values(status=outcome))
        return self.retrieve(payment_id)

    def refund(self, order, amount, key):
        try:
            return self._refund(order, amount, key)
        except IntegrityError:
            # A key used concurrently for a different payment can win its insert.
            # The failed transaction rolls back the balance change first.
            with self.db.transaction() as conn:
                prior = conn.execute(select(refunds).where(refunds.c.idempotency_key == key)).mappings().one()
                if prior["order_id"] != order["id"] or prior["amount"] != amount:
                    raise ProviderError("Refund key was already used with different parameters")
                return prior["provider_id"]

    def _refund(self, order, amount, key):
        with self.db.transaction() as conn:
            row = conn.execute(select(payments).where(payments.c.id == order["payment_id"]).with_for_update()).mappings().one()
            prior = conn.execute(select(refunds).where(refunds.c.idempotency_key == key)).mappings().first()
            if prior:
                if prior["order_id"] != order["id"] or prior["amount"] != amount:
                    raise ProviderError("Refund key was already used with different parameters")
                return prior["provider_id"]
            if row["status"] != "succeeded" or amount > row["amount"] - row["refunded"]:
                raise ProviderError("Refund exceeds captured funds or payment has not succeeded")
            provider_id = "re_demo_" + uuid.uuid4().hex[:20]
            conn.execute(payments.update().where(payments.c.id == row["id"]).values(refunded=row["refunded"] + amount))
            conn.execute(refunds.insert().values(idempotency_key=key, order_id=order["id"], amount=amount, provider_id=provider_id, created_at=time.time()))
            return provider_id


class StripeTestProvider:
    """Optional real Stripe sandbox adapter. Never accepts a live secret key."""
    name = "stripe_test"

    def __init__(self, secret):
        if not secret.startswith("sk_test_"):
            raise ValueError("Only Stripe test keys are supported")
        self.secret = secret

    def _request(self, method, path, *, data=None, params=None, key=None):
        headers = {"Idempotency-Key": key} if key else {}
        try:
            response = httpx.request(method, "https://api.stripe.com/v1/" + path,
                auth=(self.secret, ""), headers=headers, data=data, params=params, timeout=10)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("Stripe request failed; retry with the same idempotency key") from exc
        if response.status_code >= 400:
            # Deliberately avoid logging keys, client secrets, or response objects.
            code = body.get("error", {}).get("code", "provider_error")
            raise ProviderError("Stripe returned " + str(code)[:80])
        if body.get("livemode") is True:
            raise ProviderError("Live Stripe objects are not supported")
        return body

    def create(self, order):
        return self._request("POST", "payment_intents", key="create:" + order["id"], data={
            "amount": str(order["amount"]), "currency": order["currency"],
            "payment_method": "pm_card_visa", "payment_method_types[]": "card", "confirm": "true",
            "metadata[payflow_order_id]": order["id"], "description": order["product"],
        })

    def retrieve(self, payment_id):
        obj = self._request("GET", "payment_intents/" + payment_id,
                            params={"expand[]": "latest_charge"})
        charge = obj.get("latest_charge")
        return dict(id=obj["id"], amount=obj["amount"], currency=obj["currency"],
                    status=obj["status"], failed=bool(obj.get("last_payment_error")),
                    refunded=charge.get("amount_refunded", 0) if isinstance(charge, dict) else 0)

    def refund(self, order, amount, key):
        return self._request("POST", "refunds", key="refund:" + order["id"] + ":" + key,
            data={"payment_intent": order["payment_id"], "amount": str(amount)})["id"]
