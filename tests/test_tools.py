from scripts.demo import run_demo
from scripts.cloudpulse_check import check_service


def test_demo_cli_runs_all_service_scenarios(client):
    report = run_demo(client)
    assert report["passed"] and len(report["scenarios"]) == 7


def test_monitor_probe_detects_backlog(client, service, order_data):
    assert check_service(client)["healthy"]
    order = service.create_order(order_data, "monitor")["order"]
    service.set_control(paused=True)
    service.emit(order["id"])
    report = check_service(client, max_backlog=0)
    assert not report["healthy"] and report["backlog"] == 1
    service.set_control(paused=False)
    service.drain()
    assert check_service(client, max_backlog=0)["healthy"]
