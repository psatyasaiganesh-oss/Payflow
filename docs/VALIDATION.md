# Build validation — 10 October 2026

## Executed checks

The build used Python 3.12 and Node 24.19.0 in this environment. Final test counts are recorded below after the last run. The suite covers durable acknowledgment, request/event idempotency, concurrent creation and refunds, multiple workers, balance constraints, stale event handling, signature failures, retry/dead-event recovery, authentication, monitoring probes and HTTP adapter contracts.

| Check | Result |
| --- | --- |
| Python backend tests | 46 passed |
| JavaScript companion-model tests | 11 passed |
| Dashboard JavaScript syntax | Passed |
| Local replay benchmark | Correctness passed: 100 requests / eight threads, one order; ten deliveries, one capture |
| Companion site deployment | Succeeded; private audience retained |

Commands:

```bash
python -m pytest -q
node --test tests/demo-store.test.mjs
node --check web/app.js
python -m scripts.benchmark --requests 100 --workers 8
```

One upstream test-client deprecation warning is emitted by the installed FastAPI/Starlette version about its `httpx` transport. Tests completed successfully.

## Benchmark observation

One local run reported a 0.2772-second wall time, 8.49 ms median and 127.01 ms p95 across 100 concurrent idempotent API requests with eight threads. Its observed rate was 360.69 requests/second for that short in-process run. These numbers include test-client and local database behavior, fluctuate between runs, and are not capacity estimates. The meaningful invariant is that the request/event replays produced one order and one capture.

## What was not exercised

- PostgreSQL and Docker Compose were not run: their runtimes were unavailable. The supplied GitHub Actions job runs the Python suite against PostgreSQL 16, but no GitHub CI run is claimed here.
- The Stripe adapter's network boundary was mocked in tests. A real Stripe sandbox account/CLI integration was not configured or run.
- Dashboard routes/assets were served through FastAPI tests and the JavaScript model was tested. No browser rendering or interactive visual QA was available.
- Windows and Python 3.11 were not run locally. Their documented commands/CI matrix are provided for subsequent verification.

The hosted site contains only the companion browser simulation; it does not deploy PostgreSQL or the Python API. The archive contains the implementation and its configuration so you can run those paths locally.
