# PayFlow — Payment Reliability Lab

A Python portfolio project that answers a practical question: **what happens when a payment request is repeated, a webhook arrives twice, or a worker crashes?**

PayFlow stores incoming events before acknowledging them, processes them with recoverable worker leases, and commits order fulfillment and capture/refund records in one transaction. A dashboard makes those decisions visible.

**[Open the interactive companion demo](https://payflow-ganesh-975282.psatyasaiganesh.chatgpt.site)** — private to your current account. This hosted version uses synthetic payments and browser storage. Run this Python project for the actual database-backed implementation.

## What you can demonstrate

| Capability | Working implementation |
| --- | --- |
| Request idempotency | Persistent request keys; conflicting parameters return HTTP 409 |
| Webhook authenticity | HMAC validation of raw bytes; five-minute timestamp tolerance |
| Durable event inbox | Database commit before acknowledgment; duplicate delivery counts |
| Business-effect deduplication | Unique capture/refund effect keys and locked order updates |
| Worker recovery | Claim tokens, expiring leases, exponential retries, dead-event recovery |
| Money handling | Integer minor units; partial/full refunds; separate INR/USD totals |
| Reconciliation | Fetch provider truth and repair missing capture/refund confirmations |
| Monitoring | Readiness, Prometheus-format metrics, recent API p95, CloudPulse probe |
| Failure lab | Seven repeatable scenarios with checks against the resulting state |

Stack: Python 3.11+, FastAPI, SQLAlchemy, PostgreSQL or SQLite, vanilla JavaScript/CSS, pytest, Docker Compose, GitHub Actions. Redis and Node are not required to run the Python service.

## Quick start: no API keys or Docker required

Extract the archive, open a terminal in the `PayFlow` directory, and run:

```bash
python -m venv .venv
```

Activate it on macOS/Linux:

```bash
source .venv/bin/activate
```

Or in Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Then:

```bash
python -m pip install -r requirements-dev.txt
python -m uvicorn payflow.app:create_app --factory --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000**. Click **Load sample data**, then explore Payments, Event inbox, Reliability lab, and Monitoring. API documentation is at **http://127.0.0.1:8000/docs**.

The default is a persistent local simulator with a `payflow.db` SQLite file. This starts an automatic worker inside the API process. Requests and events remain in the database after restarting the application.

In a second terminal, with the same virtual environment active:

```bash
python -m scripts.demo
python -m scripts.cloudpulse_check
```

The demo runs every scenario against the running service. The monitoring probe prints JSON and exits with status 0 for healthy or 1 for unhealthy.

## PostgreSQL and separate worker

With Docker and Docker Compose installed:

```bash
docker compose up --build
```

Open the same dashboard URL. Compose starts PostgreSQL 16, the API, and a separate worker. The API starts first and initializes the schema; the worker starts after API readiness succeeds. The API port is bound to localhost and PostgreSQL is internal to the Compose network. A named volume preserves the database when you stop the containers.

```bash
docker compose down
```

For a manually managed database, set `DATABASE_URL` to a `postgresql+psycopg://` URL. To run a separate worker, set `PAYFLOW_AUTO_WORKER=false` in the API environment and start this in another terminal with the same database/provider environment:

```bash
python -m payflow.worker
```

The Docker/PostgreSQL path is supplied with a CI job; it was not executed in the build environment. See [validation notes](docs/VALIDATION.md).

## Verification

```bash
python -m pytest -q
node --test tests/demo-store.test.mjs
node --check web/app.js
python -m scripts.benchmark --requests 100 --workers 8
```

Node 22+ is needed only for the hosted companion model's tests. The benchmark uses FastAPI's in-process test client and temporary SQLite. It measures this machine's local replay workload, not external Stripe latency or production throughput.

To run the Python tests against an existing PostgreSQL instance, set `PAYFLOW_TEST_DATABASE_URL` first. Tests create a separate temporary schema per test and drop it afterward; the database user needs schema creation permission.

## API example

Amounts are minor units: `149900` means ₹1,499.00 for INR or $1,499.00 for USD.

```bash
curl -X POST http://127.0.0.1:8000/api/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: checkout-demo-001' \
  -d '{"product":"Developer Pro","customer":"Ganesh","amount":149900,"currency":"inr"}'
```

Repeat the exact request to receive the same order. Change the amount while keeping the same key to receive HTTP 409. Use the returned order ID with `/api/demo/orders/{order_id}/emit` to deliver a simulator success event.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/orders` | Create/replay a test order; `Idempotency-Key` required |
| `POST /api/orders/{id}/refund` | Refund with a request key and amount |
| `POST /webhooks/stripe` | Receive a signed, explicitly test-mode event |
| `GET /api/state` | Dashboard state, totals, event history and recent monitoring |
| `POST /api/reconcile` | Repair missing capture/refund confirmations |
| `POST /api/worker/pause`, `/resume`, `/run` | Control the simulator's queue |
| `POST /api/events/{id}/retry` | Reschedule a pending/dead event |
| `POST /api/demo/scenarios/{name}` | Execute a failure scenario |
| `GET /health/live`, `/health/ready`, `/metrics` | Monitoring endpoints |

## Optional Stripe sandbox adapter

The project runs without Stripe. The optional adapter makes HTTP calls to Stripe's API and accepts only `sk_test_` keys. Its external integration has **not** been run with a real account; adapter tests mock the HTTP boundary.

To configure it, set these environment variables in your local terminal:

| Variable | Value |
| --- | --- |
| `PAYFLOW_PROVIDER` | `stripe_test` |
| `STRIPE_SECRET_KEY` | Your sandbox secret key beginning `sk_test_` |
| `STRIPE_WEBHOOK_SECRET` | Your actual endpoint or Stripe CLI signing secret |
| `PAYFLOW_API_TOKEN` | A randomly generated access token |

Generate a local token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Set variables with `export NAME=value` on bash or `$env:NAME = 'value'` in PowerShell. The direct Python app reads environment variables; it does not automatically load `.env`. Compose does load `.env`; `.env.example` lists the relevant values.

With the Stripe CLI installed and logged into your test environment:

```bash
stripe listen --forward-to http://127.0.0.1:8000/webhooks/stripe
```

Set the signing secret printed by that listener **before starting the API**. Open the dashboard's connection dialog and enter your PayFlow API token. Creating an order confirms a test PaymentIntent using `pm_card_visa`; the webhook worker fetches the current PaymentIntent before updating the order. Simulator seed/failure controls are disabled in Stripe mode. There is no real-card entry or hosted Checkout flow in this version.

Official references: [webhooks](https://docs.stripe.com/webhooks), [idempotent requests](https://docs.stripe.com/api/idempotent_requests), [PaymentIntents](https://docs.stripe.com/api/payment_intents/create), [refunds](https://docs.stripe.com/api/refunds/create), [testing](https://docs.stripe.com/testing).

## Project map

| Path | What to read |
| --- | --- |
| `payflow/service.py` | Order idempotency, inbox, worker transactions, reconciliation |
| `payflow/providers.py` | Persistent simulator and optional Stripe test adapter |
| `payflow/database.py` | Tables, uniqueness/balance constraints and transaction setup |
| `payflow/security.py` | Raw-byte signature validation |
| `payflow/app.py` | HTTP API, authorization, dashboard and monitoring |
| `web/` | Dashboard shared by the local API and hosted companion demo |
| `tests/` | Failure, concurrency, API-boundary and companion model tests |
| `scripts/` | Walkthrough, monitoring probe and local replay benchmark |
| `docs/` | Architecture, demo script, interview notes and validation |

Start with [the two-minute demo](docs/DEMO_GUIDE.md), then read [architecture](docs/ARCHITECTURE.md) and [interview notes](docs/INTERVIEW_NOTES.md). The [CloudPulse integration guide](docs/CLOUDPULSE.md) connects this project to your monitoring work.

## Scope and next contributions

PayFlow is an independent portfolio lab. Fulfillment is represented by a persistent count and audit entry; it does not ship goods or email customers. The capture/refund journal is a simple movement ledger, not complete double-entry accounting. The hosted browser simulator cannot provide the Python backend's transaction/concurrency guarantees.

The worker intentionally uses a global claim lock for a small, understandable implementation. Schema creation uses SQLAlchemy `create_all`; versioned migrations, high-volume pagination, a worker heartbeat, richer structured logs, and a real sandbox integration run are worthwhile next contributions. Readiness verifies database connectivity, not a separate worker's liveness. Browser rendering was not visually verified during this build.

This project gives you concrete engineering decisions to explain in screening. It does not determine Stripe internship eligibility or guarantee an interview.
