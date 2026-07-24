#!/usr/bin/env python3
import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


def request_json(base_url, path, method="GET", payload=None, timeout=5.0):
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=body, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
            return True, response.status, data, (time.perf_counter() - started) * 1000
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, None, {"error": f"{type(exc).__name__}: {exc}"}, (time.perf_counter() - started) * 1000


def percentile(values, percentage):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentage)))
    return ordered[index]


def main():
    parser = argparse.ArgumentParser(description="Smart-home final acceptance test")
    parser.add_argument("--url", default="http://127.0.0.1:5000")
    parser.add_argument("--count", type=int, default=100, help="mailbox ping count")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    base_url = args.url.rstrip("/")

    checks = []
    for path in ("/api/auth/status", "/api/state", "/api/health", "/api/overview", "/api/realtime/status"):
        ok, status, data, latency = request_json(base_url, path, timeout=args.timeout)
        checks.append({
            "path": path,
            "success": ok and status == 200,
            "http_status": status,
            "latency_ms": round(latency, 3),
            "message": data.get("message", "") if isinstance(data, dict) else "",
        })

    latencies = []
    failures = []
    for index in range(max(1, args.count)):
        ok, status, data, latency = request_json(
            base_url,
            "/api/realtime/command",
            method="POST",
            payload={"command": "ping", "payload": {"source": "acceptance", "sequence": index + 1}},
            timeout=args.timeout,
        )
        if ok and status == 200 and data.get("success"):
            latencies.append(latency)
        else:
            failures.append({"sequence": index + 1, "status": status, "response": data})

    total = max(1, args.count)
    report = {
        "project": "Phytium smart-home heterogeneous control system",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "base_url": base_url,
        "read_only_checks": checks,
        "mailbox_ping": {
            "total": total,
            "success": len(latencies),
            "failed": len(failures),
            "success_rate": round(len(latencies) / total, 4),
            "average_latency_ms": round(statistics.fmean(latencies), 3) if latencies else None,
            "p95_latency_ms": round(percentile(latencies, 0.95), 3) if latencies else None,
            "max_latency_ms": round(max(latencies), 3) if latencies else None,
            "failures": failures[:20],
        },
        "passed": all(check["success"] for check in checks) and not failures,
    }

    output = Path(args.output) if args.output else Path("logs") / f"acceptance-{datetime.now():%Y%m%d-%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nreport: {output.resolve()}")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
