"""Run all seven backend scenarios against an already-running mock service."""
import argparse
import json
import os
import sys
import httpx

SCENARIOS = ["duplicate_request", "duplicate_webhook", "out_of_order", "worker_failure",
             "crash_recovery", "reconciliation", "invalid_signature"]


def run_demo(client):
    client.post("/api/demo/seed", json={}).raise_for_status()
    reports = []
    for name in SCENARIOS:
        response = client.post("/api/demo/scenarios/" + name, json={})
        response.raise_for_status()
        reports.append(response.json())
    return {"passed": all(report["passed"] for report in reports), "scenarios": reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    token = os.getenv("PAYFLOW_API_TOKEN", "")
    headers = {"Authorization":"Bearer " + token} if token else {}
    try:
        with httpx.Client(base_url=args.url.rstrip("/"), headers=headers, timeout=60) as client:
            report = run_demo(client)
        print(json.dumps(report, indent=2))
        return 0 if report["passed"] else 1
    except (httpx.HTTPError, ValueError) as exc:
        print("Demo failed: " + type(exc).__name__ + ". Check the service URL and token.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
