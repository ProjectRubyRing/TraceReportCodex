#!/usr/bin/env python3
"""Create synthetic, credential-free GetQueryResults data for offline verification."""
import datetime as dt
import json
from pathlib import Path

BASE = 1792972800  # 2026-10-26T00:00:00Z
TRACE_A = "69000000111111112222222233333333"
TRACE_B = "69000000444444445555555566666666"
TRACE_C = "69000000777777778888888899999999"


def record(trace, identifier, parent, service, name, offset, duration, code=200, kind="SERVER", extra=None):
    start = BASE * 1_000_000_000 + offset * 1_000_000 + 123
    data = {
        "traceId": trace, "spanId": f"{identifier:016x}", "parentSpanId": f"{parent:016x}" if parent else "",
        "name": name, "kind": kind, "startTimeUnixNano": start,
        "endTimeUnixNano": start + duration * 1_000_000, "durationNano": duration * 1_000_000,
        "resource": {"attributes": {"service.name": service, "deployment.environment.name": "sample"}},
        "attributes": {"aws.local.service": service, "http.request.method": "POST", "http.response.status_code": code},
        "status": {"code": "ERROR" if code >= 400 else "OK"},
    }
    data["attributes"].update(extra or {})
    if code >= 500:
        data["events"] = [{"name": "exception", "attributes": {"exception.type": "ExampleTimeout", "exception.message": "合成データ: 接続タイムアウト"}}]
    timestamp = dt.datetime.fromtimestamp(BASE + offset / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return {"@timestamp": timestamp, "@message": json.dumps(data, ensure_ascii=False, separators=(",", ":")), "@ptr": f"sample-{identifier}"}


def sample_records():
    return [
        record(TRACE_A, 1, 0, "web-gateway", "POST /checkout", 0, 1400, 500),
        record(TRACE_A, 2, 1, "order-service", "注文処理", 20, 1250, 500),
        record(TRACE_A, 3, 2, "order-service", "POST payment", 100, 900, 500, "CLIENT", {"aws.remote.service": "payment-service"}),
        record(TRACE_A, 4, 3, "payment-service", "決済処理", 150, 800, 500),
        record(TRACE_A, 5, 4, "payment-service", "SELECT inventory", 200, 120, 200, "CLIENT", {"db.system.name": "postgresql", "db.namespace": "sample_inventory"}),
        record(TRACE_A, 6, 2, "inventory-service", "在庫照会", 1100, 150),
        record(TRACE_B, 7, 0, "web-gateway", "GET /health", 2000, 40),
        record(TRACE_B, 8, 7, "health-service", "ヘルスチェック", 2005, 20),
        record(TRACE_C, 9, 999, "worker-service", "親スパンが範囲外の処理", 3000, 250, 429),
    ]


def main():
    rows = [[{"field": key, "value": value} for key, value in row.items()] for row in reversed(sample_records())]
    result = {"status": "Complete", "results": rows, "statistics": {"recordsMatched": len(rows), "recordsScanned": len(rows), "bytesScanned": 8192}}
    path = Path(__file__).with_name("insights-results.json")
    with path.open("w", encoding="utf-8", newline="\n") as target:
        target.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(path)


if __name__ == "__main__":
    main()
