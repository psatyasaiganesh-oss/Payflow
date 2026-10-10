"""One-shot CloudPulse-compatible HTTP probe; returns JSON and an exit status."""
import argparse
import json
import os
import time
import httpx


def check_service(client, max_backlog=20, max_age=60):
    started = time.perf_counter()
    ready = client.get("/health/ready")
    ready.raise_for_status()
    state = client.get("/api/state")
    state.raise_for_status()
    stats = state.json()["stats"]
    checks = {
        "database_ready": ready.json().get("database") == "connected",
        "no_dead_events": stats["dead_events"] == 0,
        "backlog_within_limit": stats["backlog"] <= max_backlog,
        "queue_age_within_limit": stats["oldest_pending_seconds"] <= max_age,
    }
    return {"healthy": all(checks.values()), "checks":checks,
        "probe_ms":round((time.perf_counter()-started)*1000, 2),
        "backlog":stats["backlog"], "dead_events":stats["dead_events"],
        "oldest_pending_seconds":stats["oldest_pending_seconds"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--max-backlog", type=int, default=20)
    parser.add_argument("--max-age", type=int, default=60)
    args = parser.parse_args()
    token = os.getenv("PAYFLOW_API_TOKEN", "")
    headers = {"Authorization":"Bearer " + token} if token else {}
    try:
        with httpx.Client(base_url=args.url.rstrip("/"), headers=headers, timeout=5) as client:
            report = check_service(client, args.max_backlog, args.max_age)
    except (httpx.HTTPError, ValueError, KeyError):
        report = {"healthy":False, "error":"service_unreachable_or_invalid_response"}
    print(json.dumps(report))
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
