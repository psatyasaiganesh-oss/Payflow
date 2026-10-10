# Explain and extend PayFlow

## A useful opening

“PayFlow explores payment correctness under retries and partial failures. A request key protects order creation. Signed webhooks enter a durable inbox, and workers update the order and money journal in one transaction. The dashboard reproduces duplicate delivery, delayed events, worker failure and missing-webhook recovery.”

Learn the code path behind this description before using it in an interview. Be precise about the difference between the hosted browser simulator, the Python implementation and the optional external adapter.

## Questions to practice

| Question | What a good answer should explain |
| --- | --- |
| Why not use an in-memory set of processed event IDs? | Restart loses it; separate processes do not share it; the database unique key persists and coordinates workers. |
| Does event deduplication guarantee one fulfillment? | Different event IDs can describe the same payment. The locked order and unique capture effect guard the business action separately. |
| When do you acknowledge a webhook? | After signature checks and durable inbox commit, before slow processing. |
| What if a worker crashes? | Its claim remains; an expired lease makes it eligible again. A token distinguishes the new owner from a late former owner. |
| Why fetch the provider state? | Event payloads can be delayed. Current state plus monotonic capture/refund logic prevents regression. |
| What if provider creation succeeds and the API times out? | Replay the persisted order and stable provider key; don't create a new order/key on uncertainty. |
| Why use integers for money? | Binary floating-point can introduce rounding differences; currency-specific minor units make balance checks exact. This version supports INR/USD only. |
| How would you deliver a purchased product? | Add a transactional outbox next to the capture, then an idempotent downstream fulfillment consumer. |
| Why does readiness not check the worker? | It currently checks database access. Queue age/depth indicate delayed processing; a separate heartbeat would improve diagnosis. |
| How would you scale it? | Replace the global claim lock with batch row claims, add pagination/retention and measure contention while preserving order/effect constraints. |

## Suggested next contributions

These are small, defensible changes that let you make the project your own:

1. Add a worker heartbeat and show “last processed” in Monitoring. Test a stale worker while the API remains healthy.
2. Add Alembic migrations for the current schema and verify upgrading an existing database preserves orders.
3. Run the PostgreSQL CI job in your own GitHub repository and resolve any environment-specific failures.
4. Configure your Stripe sandbox, capture/refund a test payment and document the actual provider event sequence.
5. Add an outbox for a simulated fulfillment consumer and prove replay does not create duplicate deliveries.

## Resume wording

Use wording that reflects what you have run and can explain. These are editable examples:

- Developed a Python/FastAPI payment reliability lab with persistent idempotency keys, signed webhooks, a durable event inbox, and transactionally deduplicated capture/refund effects.
- Added expiring worker leases, retry/dead-event recovery, and reconciliation, with seven reproducible failure scenarios covering duplicate delivery, stale events and missing confirmations.
- Exposed health and queue metrics for CloudPulse monitoring and verified concurrent order/refund replay behavior with automated tests.

After running the supplied local benchmark, you can report its correctness result: 100 repeated API requests using eight threads yielded one order, and ten webhook deliveries yielded one capture. Label it as a local in-process mock/SQLite experiment. Do not present its throughput or latency as production or Stripe performance.

Keep the optional Stripe adapter described as “implemented” until you have actually run its external integration. Add PostgreSQL validation to your resume only after running that path yourself or reviewing a successful CI run.

## Screening preparation

Know the request, claim and commit paths well enough to navigate them without memorized lines. Practice Python dictionaries/lists, sets, sorting, hashing, basic graph traversal, SQL joins/indexes/transactions, and clear debugging narration. This project provides examples of engineering judgment; internship eligibility and interview outcomes remain separate questions.
