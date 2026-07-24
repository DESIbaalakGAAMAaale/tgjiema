#!/usr/bin/env python3
"""R64 P1-10: CRDB RU 阈值门禁 CI gate。

本脚本作为发布门禁,验证 CRDB RU 消耗未超过审计阈值。
任何阻断级违规将导致 CI 失败(exit 1)。

验收标准(R64 P1-10):
    - 业务 Bot 空载 RU:0 RU/天(任何 >0 视为门禁违规)
    - 集群空载 RU 硬限:≤100 RU/天(>100 告警,>500 阻断)
    - per-DAU 限制:≤250 RU/DAU/天
    - 月度预算:≤35,000,000 RU/月

使用方法:
    # 检查今天(默认)
    python scripts/check_crdb_ru_threshold.py

    # 检查指定日期
    python scripts/check_crdb_ru_threshold.py --date 20260718

    # 检查指定月份(月度预算)
    python scripts/check_crdb_ru_threshold.py --month 202607

    # 同时检查日期 + 月份
    python scripts/check_crdb_ru_threshold.py --date 20260718 --month 202607

    # 静默模式(只输出 JSON,适合 CI)
    python scripts/check_crdb_ru_threshold.py --json

    # 仅告警模式(不阻断,只输出告警)— 调试用
    python scripts/check_crdb_ru_threshold.py --warn-only

退出码:
    0: 所有门禁通过
    1: 阻断级违规(业务 Bot RU>0 / 集群 RU>500 / 月度预算超限)
    2: 仅告警级违规(集群 RU>100)— 默认仍返回 0(告警不阻断)
       (使用 --strict-alert 时告警也返回 2)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# 将项目根目录加入 sys.path(允许从 scripts/ 直接运行)
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _print_human_report(daily_result: dict | None, monthly_result: dict | None) -> None:
    """以人类可读格式打印报告。"""
    print("═" * 70)
    print("R64 P1-10: CRDB RU 阈值门禁检查报告")
    print("═" * 70)

    if daily_result:
        print(f"\n[日级检查] 日期: {daily_result.get('date', 'N/A')}")
        print(f"  业务 Bot 空载 RU: {daily_result.get('business_bot_ru', 0)}")
        print(f"  集群总 RU:        {daily_result.get('total_ru', 0)}")
        print(f"  非 Bot 角色 RU:    {daily_result.get('non_business_ru', 0)}")
        print(f"  DAU:              {daily_result.get('dau', 0)}")
        print(f"  per-DAU RU:       {daily_result.get('per_dau_ru', 0.0):.2f}")
        thresholds = daily_result.get("thresholds", {})
        print(f"  阈值: alert={thresholds.get('alert', '?')}, "
              f"block={thresholds.get('block', '?')}, "
              f"per_dau_limit={thresholds.get('per_dau_limit', '?')}, "
              f"bot_idle_limit={thresholds.get('bot_idle_limit', '?')}")
        print(f"  通过: {'是' if daily_result.get('passed') else '否'}")
        print(f"  阻断 release: {'是' if daily_result.get('block_release') else '否'}")
        print(f"  告警: {'是' if daily_result.get('alert') else '否'}")
        violations = daily_result.get("violations", [])
        if violations:
            print(f"  违规列表:")
            for v in violations:
                print(f"    - {v}")

    if monthly_result:
        print(f"\n[月级检查] 月份: {monthly_result.get('year_month', 'N/A')}")
        print(f"  月度 RU 消耗:    {monthly_result.get('monthly_usage', 0)}")
        print(f"  月度预算:        {monthly_result.get('monthly_budget', 0)}")
        print(f"  剩余:            {monthly_result.get('remaining', 0)}")
        print(f"  使用率:          {monthly_result.get('usage_percentage', 0.0):.2f}%")
        print(f"  通过: {'是' if monthly_result.get('passed') else '否'}")
        print(f"  阻断 release: {'是' if monthly_result.get('block_release') else '否'}")
        if monthly_result.get("error"):
            print(f"  错误: {monthly_result['error']}")

    # 空载审计摘要
    try:
        from services.ru_cost_center import get_idle_crdb_audit_summary
        audit = get_idle_crdb_audit_summary()
        print(f"\n[空载 CRDB 审计摘要]")
        print(f"  策略: {audit.get('policy', '')}")
        print(f"  已审计服务: {', '.join(audit.get('audited_services', []))}")
        print(f"  允许的 CRDB 触发:")
        for trigger in audit.get("allowed_crdb_triggers", []):
            print(f"    - {trigger}")
    except Exception as e:
        print(f"\n[空载 CRDB 审计摘要] 获取失败: {e}")

    print("\n" + "═" * 70)


async def run_check(args: argparse.Namespace) -> int:
    """执行 RU 阈值门禁检查,返回退出码。"""
    daily_result: dict | None = None
    monthly_result: dict | None = None

    # CI/脚本模式下设置最小环境变量,绕过 Settings 必填字段校验
    # R64 P1-10: 本脚本仅需读取 SQLite kv_store,不需要真实 Bot Token / CRDB URL
    if not os.environ.get("SERVICE_ROLE"):
        os.environ["SERVICE_ROLE"] = "prometheus_exporter"  # 无 secrets 依赖的角色
    # R70 Wave 1: APP_ENV 必填,否则 Settings fail-closed(EnvironmentResolutionError)
    if not os.environ.get("APP_ENV"):
        os.environ["APP_ENV"] = "development"  # CI 脚本模式(非 production)
    # 提供占位值避免 Settings 校验失败(本脚本不实际使用这些值)
    for var in ("UPLOAD_BOT_TOKEN", "DECODER_BOT_TOKEN", "SENDER_BOT_TOKEN",
                "MON_BOT_TOKEN", "ADMIN_BOT_TOKEN", "COCKROACHDB_URL"):
        if not os.environ.get(var):
            os.environ[var] = f"ci-placeholder-{var.lower()}"

    # 初始化 cache_store(若 DB 不存在,跳过数据查询)
    # 注意:本脚本仅读取现有 kv_store 数据,不调用 init()(避免触发完整迁移)
    # 若 cache_store 未初始化(_db is None),使用只读 SQLite 直连
    try:
        # 设置 cache_store DB 路径(允许 CI 通过环境变量覆盖)
        if args.cache_db:
            os.environ["CACHE_STORE_DB"] = str(args.cache_db)
        from database.cache_store import get_cache_store
        store = get_cache_store()
        # 若 store._db 已初始化(由其他进程或先前调用初始化),直接使用
        # 否则降级为只读 SQLite 连接(由 ru_cost_center 内部 try/except 处理)
        if getattr(store, "_db", None) is None:
            if not args.json:
                print("INFO: cache_store 未初始化,将以只读模式尝试读取现有数据",
                      file=sys.stderr)
    except Exception as e:
        if not args.json:
            print(f"WARN: cache_store 初始化失败(将以零值进行门禁检查): {e}",
                  file=sys.stderr)

    # 日级检查
    if not args.month_only:
        try:
            from services.ru_cost_center import check_daily_threshold
            daily_result = await check_daily_threshold(args.date)
        except Exception as e:
            if not args.json:
                print(f"ERROR: 日级检查异常: {e}", file=sys.stderr)
            daily_result = {
                "date": args.date or "",
                "passed": False,
                "block_release": True,  # 检查失败视为阻断(.fail-closed)
                "alert": True,
                "violations": [f"日级检查异常: {e}"],
                "business_bot_ru": 0,
                "total_ru": 0,
                "non_business_ru": 0,
                "dau": 0,
                "per_dau_ru": 0.0,
                "thresholds": {},
                "error": str(e),
            }

    # 月级检查
    if args.month or not args.day_only:
        try:
            from services.ru_cost_center import check_monthly_budget
            month_str = args.month
            monthly_result = await check_monthly_budget(month_str)
        except Exception as e:
            if not args.json:
                print(f"ERROR: 月级检查异常: {e}", file=sys.stderr)
            monthly_result = {
                "year_month": args.month or "",
                "passed": False,
                "block_release": True,
                "monthly_usage": 0,
                "monthly_budget": 0,
                "remaining": 0,
                "usage_percentage": 0.0,
                "error": str(e),
            }

    # 决定退出码
    block = False
    alert = False
    if daily_result:
        if daily_result.get("block_release"):
            block = True
        if daily_result.get("alert"):
            alert = True
    if monthly_result:
        if monthly_result.get("block_release"):
            block = True

    if args.json:
        # JSON 输出模式
        output = {
            "daily": daily_result,
            "monthly": monthly_result,
            "block_release": block,
            "alert": alert,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        _print_human_report(daily_result, monthly_result)

    if block:
        if not args.warn_only:
            print("\n❌ 阻断级违规:CRDB RU 超过阻断阈值,release 已被门禁拦截",
                  file=sys.stderr)
            return 1
        else:
            print("\n⚠️  阻断级违规(已被 --warn-only 跳过,不阻断 CI)",
                  file=sys.stderr)
            return 0
    if alert and args.strict_alert:
        print("\n⚠️  告警级违规(--strict-alert 模式下视为失败)", file=sys.stderr)
        return 2
    if not args.json:
        print("\n✅ CRDB RU 门禁检查通过")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="R64 P1-10: CRDB RU 阈值门禁 CI gate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--date", default=None,
        help="检查指定日期 YYYYMMDD(默认今天)",
    )
    parser.add_argument(
        "--month", default=None,
        help="检查指定月份 YYYYMM(默认当月)",
    )
    parser.add_argument(
        "--day-only", action="store_true",
        help="只检查日级门禁(跳过月度预算检查)",
    )
    parser.add_argument(
        "--month-only", action="store_true",
        help="只检查月度预算(跳过日级门禁)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="JSON 输出模式(适合 CI 解析)",
    )
    parser.add_argument(
        "--warn-only", action="store_true",
        help="告警模式(不阻断,即使有违规也返回 0)— 调试用",
    )
    parser.add_argument(
        "--strict-alert", action="store_true",
        help="严格模式(告警级违规也返回非零退出码)",
    )
    parser.add_argument(
        "--cache-db", default=None,
        help="指定 cache_store.db 路径(默认使用环境变量或项目 data 目录)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(run_check(args))


if __name__ == "__main__":
    sys.exit(main())
