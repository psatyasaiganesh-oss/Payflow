# Connecting PayFlow to CloudPulse

The project provides a monitoring interface for your CloudPulse work. Your existing CloudPulse repository was not modified; use its current HTTP-monitor configuration to point at these endpoints or wrap the supplied probe.

## HTTP monitors

| Endpoint | Healthy response | What it covers |
| --- | --- | --- |
| `/health/live` | HTTP 200, `status: alive` | API process responds |
| `/health/ready` | HTTP 200, `database: connected` | API can query its database |
| `/api/state` | HTTP 200; inspect `stats` | Queue depth, age, failures and delivery counts |
| `/metrics` | HTTP 200; text metrics | Prometheus-format integration |

Business API and metrics routes require `Authorization: Bearer <token>` when `PAYFLOW_API_TOKEN` is set. Health endpoints are public. Store the token in your monitor's environment/secret configuration. The companion demo is a static site and does not serve these Python API endpoints.

## Probe

Against a local service:

```bash
python -m scripts.cloudpulse_check --url http://127.0.0.1:8000 --max-backlog 20 --max-age 60
```

The probe inherits `PAYFLOW_API_TOKEN`, fetches readiness and state, and returns JSON. Treat exit status 1 as unhealthy. It checks database readiness, zero dead events, a backlog threshold and an oldest-event age threshold. The thresholds are demonstration defaults, not established service objectives.

From a separate container, `127.0.0.1` points to that container; use `http://api:8000` when the monitor shares the PayFlow Compose network. From a different machine, use the backend's reachable origin and its configured access token.

## Metrics

| Metric | Meaning |
| --- | --- |
| `payflow_orders` | Total known orders |
| `payflow_captured_orders` | Orders fulfilled/captured at least once |
| `payflow_failed_orders` | Orders currently marked failed |
| `payflow_backlog` | Pending/claimed event count |
| `payflow_dead_events` | Events that exhausted automatic attempts |
| `payflow_events_processed` | Events completed by the worker |
| `payflow_duplicate_deliveries` | Deliveries after the first copy of each event |
| `payflow_oldest_pending_seconds` | Age of the oldest pending/claimed event |

These values are gauges derived from database state. The API's recent request p95 is shown in `/api/state` and the dashboard, not exported as a Prometheus histogram.

## Show the integration working

Pause the worker, deliver a payment event, and run the probe with `--max-backlog 0`. It should report unhealthy. Resume processing, then run it again and show the recovery. Add a monitor chart for queue age if your CloudPulse UI supports custom values. Readiness alone cannot detect an idle or crashed separate worker; backlog and age provide the useful operational signal in this version.
