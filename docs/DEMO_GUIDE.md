# A two-minute interview demonstration

Use the local Python app when demonstrating backend guarantees. The hosted version is useful for exploring the interface, but its storage and worker model run in the browser.

## Prepare

Start the application as described in the README. Click **Load sample data**. Keep the API docs and `payflow/service.py` available. Explain that the amounts are synthetic test payments and that fulfillment is represented by a database count.

## Walkthrough

| Time | Action | Explain |
| --- | --- | --- |
| 0:00–0:25 | Open Payments; create a successful payment and inspect it | “The order and provider payment are persisted. Monetary amounts are integer minor units.” |
| 0:25–0:50 | In Reliability lab, run Duplicate webhooks | “Ten deliveries refer to one event. The inbox tracks them while the order is fulfilled once.” |
| 0:50–1:10 | Inspect Event inbox and the order's capture ledger | “Acknowledgment follows inbox commit. The order update and capture entry commit together.” |
| 1:10–1:30 | Run Worker retry and Crash recovery | “A transient failure retries; an abandoned claim becomes eligible after its lease expires. This lab controls the timing.” |
| 1:30–1:50 | Run Missing webhook; open Monitoring | “Reconciliation reads provider truth and repairs an unconfirmed capture through the same worker.” |
| 1:50–2:00 | Show test output and explain one tradeoff | “The suite exercises concurrency and failure recovery. I chose a database inbox to keep the critical transaction understandable.” |

## Seven scenarios

| Scenario ID | Trigger | Expected evidence |
| --- | --- | --- |
| `duplicate_request` | Replay one request 20 times | One order and provider payment |
| `duplicate_webhook` | Deliver one event 10 times | One capture; delivery count 10 |
| `out_of_order` | Deliver a stale failure after successful capture | Order remains paid; one capture |
| `worker_failure` | Inject failure on the next claim; retry | At least two attempts; eventually paid |
| `crash_recovery` | Leave a claim unfinished; expire its lease | Reclaimed event completes once |
| `reconciliation` | Settle provider payment without a webhook | Reconciliation repairs local capture |
| `invalid_signature` | Modify the signed event body | Rejected event; no inbox/fulfillment effect |

## A monitoring story

1. Pause the worker from Event inbox.
2. Create a successful test payment. Its event waits; the order remains pending.
3. Run `python -m scripts.cloudpulse_check --max-backlog 0`. It reports unhealthy because one event is waiting.
4. Resume the worker. The order becomes paid and the backlog reaches zero.
5. Repeat the probe and show recovery.

The probe is one-shot. Schedule it through your monitoring system if you want recurring checks.

## A refund story

Create a ₹1,499 successful order, request a ₹499 refund, and inspect the ledger. The net is ₹1,000 and fulfillment remains one. The automated tests also replay refund keys and submit competing refund requests to verify the balance cannot change twice or exceed the captured amount.

## Reset

For a fresh local workspace, stop the app and move its `payflow.db` file to a backup location, then restart. Existing data is otherwise kept across restarts. The companion demo uses browser storage under a PayFlow-specific key; clearing this site's local storage resets its synthetic workspace.
