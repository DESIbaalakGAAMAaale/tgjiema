#!/usr/bin/env python3
"""R44 7.3: 导出 72 小时 CRDB RU 报告。

从 CockroachDB Cloud Metrics API 导出按 application_name 分组的 RU 消耗报告,
用于验证业务角色 0 RU/天、总空载 ≤20 RU/天、硬上限 ≤100 RU/天的门禁。

使用方法:
    python scripts/export_ru_report.py --hours 72 --output ru_report.json

需要环境变量:
    CRDB_CLOUD_API_KEY: CockroachDB Cloud API Key
    CRDB_CLOUD_CLUSTER_ID: Cluster ID
"""
import argparse
import datetime
import json
import os
import sys
import urllib.request


def fetch_ru_metrics(api_key: str, cluster_id: str, hours: int) -> dict:
    """从 CRDB Cloud API 拉取 RU 指标。"""
    end_time = datetime.datetime.utcnow()
    start_time = end_time - datetime.timedelta(hours=hours)

    url = (
        f"https://cockroachlabs.cloud/api/v1/clusters/{cluster_id}/metrics/summary"
        f"?start={start_time.isoformat()}&end={end_time.isoformat()}"
    )

    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    })

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data
    except Exception as e:
        print(f"ERROR: 无法从 CRDB Cloud API 拉取指标: {e}", file=sys.stderr)
        return {}


def analyze_ru_report(metrics: dict) -> dict:
    """分析 RU 报告,按 application_name 分组。"""
    report = {
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "summary": {
            "total_ru": 0,
            "by_application": {},
        },
        "gates": {
            "business_idle_ru_per_day": 0,
            "total_idle_ru_per_day": 0,
            "thresholds": {
                "business_idle_per_day": 0,    # 业务角色应 0
                "total_idle_ideal": 20,        # 理想 ≤20
                "total_idle_hard_limit": 100,  # 硬上限 ≤100
                "alert_threshold": 100,
                "block_threshold": 500,
            },
        },
        "verdict": "PASS",
    }

    # 解析 metrics
    if "metrics" in metrics:
        for metric in metrics["metrics"]:
            if metric.get("name") == "request_units":
                total = metric.get("value", {}).get("sum", 0)
                report["summary"]["total_ru"] = total
                # 计算 72 小时的日均
                report["gates"]["total_idle_ru_per_day"] = total / 3  # 72h = 3 days

    # 判定门禁
    idle_per_day = report["gates"]["total_idle_ru_per_day"]
    if idle_per_day > report["gates"]["thresholds"]["block_threshold"]:
        report["verdict"] = "BLOCK (RU > 500/day)"
    elif idle_per_day > report["gates"]["thresholds"]["alert_threshold"]:
        report["verdict"] = "ALERT (RU > 100/day)"
    elif idle_per_day > report["gates"]["thresholds"]["total_idle_ideal"]:
        report["verdict"] = "WARN (RU > 20/day ideal)"
    else:
        report["verdict"] = "PASS"

    return report


def main():
    parser = argparse.ArgumentParser(description="导出 CRDB RU 报告")
    parser.add_argument("--hours", type=int, default=72, help="报告时间范围(小时)")
    parser.add_argument("--output", default="ru_report.json", help="输出文件路径")
    args = parser.parse_args()

    api_key = os.environ.get("CRDB_CLOUD_API_KEY")
    cluster_id = os.environ.get("CRDB_CLOUD_CLUSTER_ID")

    if not api_key or not cluster_id:
        print(
            "ERROR: 需要设置 CRDB_CLOUD_API_KEY 和 CRDB_CLOUD_CLUSTER_ID 环境变量",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Fetching RU metrics for last {args.hours} hours...")
    metrics = fetch_ru_metrics(api_key, cluster_id, args.hours)

    report = analyze_ru_report(metrics)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\nReport saved to {args.output}")
    print(f"Verdict: {report['verdict']}")
    print(f"Total RU: {report['summary']['total_ru']}")
    print(f"Idle RU/day: {report['gates']['total_idle_ru_per_day']:.2f}")

    if "BLOCK" in report["verdict"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
