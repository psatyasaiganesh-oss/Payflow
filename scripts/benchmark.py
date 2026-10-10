"""Measure local, in-process idempotent API replays. This is not a network load test."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import tempfile
import time
from fastapi.testclient import TestClient
from payflow.app import create_app
from payflow.config import Settings


def benchmark(requests=100, workers=8):
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(Settings(database_url="sqlite:///" + str(Path(directory)/"bench.db"), auto_worker=False))
        with TestClient(app) as client:
            data = {"product":"Benchmark", "customer":"Synthetic customer", "amount":149900, "currency":"inr"}
            def replay(_):
                started = time.perf_counter()
                response = client.post("/api/orders", json=data, headers={"Idempotency-Key":"benchmark-replay"})
                response.raise_for_status()
                return (time.perf_counter()-started)*1000, response.json()["order"]["id"]
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(replay, range(requests)))
            wall = time.perf_counter()-started
            latencies = sorted(result[0] for result in results)
            order_id = results[0][1]
            response = client.post(f"/api/demo/orders/{order_id}/emit", json={"outcome":"success", "repetitions":10})
            response.raise_for_status()
            state = client.get("/api/state").json()
            captures = [entry for entry in state["ledger"] if entry["kind"] == "capture"]
            return {"scope":"in-process ASGI, mock provider, temporary SQLite, this machine only",
                "requests":requests, "workers":workers, "wall_seconds":round(wall, 4),
                "p50_ms":round(statistics.median(latencies), 2),
                "p95_ms":round(latencies[min(len(latencies)-1, int(len(latencies)*0.95))], 2),
                "observed_requests_per_second":round(requests/wall, 2),
                "unique_orders":len({result[1] for result in results}),
                "capture_entries":len(captures), "duplicate_deliveries":state["stats"]["duplicate_deliveries"],
                "correctness_passed":len({result[1] for result in results}) == len(captures) == 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.requests <= 10000 or not 1 <= args.workers <= 64:
        parser.error("Choose 1–10000 requests and 1–64 workers")
    report = benchmark(args.requests, args.workers)
    print(json.dumps(report, indent=2))
    return 0 if report["correctness_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
