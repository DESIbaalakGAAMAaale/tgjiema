"""R40 §9.3: RU 成本中心 — 按服务/功能/千次操作计算 RU。

职责:
    记录与统计 CockroachDB RU(请求单元)消耗,支持:
    1. 实时记录各服务/操作的 RU 使用(写入 kv_store)
    2. 按日/按服务/按操作类型聚合统计
    3. 每千次操作的 RU 成本估算
    4. RU 预算管理与告警

设计原则:
    - 纯函数式 + async
    - 通过 database.cache_store.get_cache_store() 获取单例
    - RU 数据存 kv_store,key='ru_usage:{YYYYMMDD}:{service}',value=JSON
    - 聚合统计零 CRDB RU(全部走 SQLite kv_store)
    - 中文注释,loguru 日志

RU 单价参考(CockroachDB Cloud 定价):
    - 每次读 1 RU
    - 每次写 2 RU
    - 每次查询 3 RU(含网络/计算)
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store


# ─── RU 单价估算(基于 CockroachDB Cloud 定价) ────────────────
RU_PER_READ = 1       # 每次读 1 RU
RU_PER_WRITE = 2     # 每次写 2 RU
RU_PER_QUERY = 3     # 每次查询 3 RU

# ─── 服务列表 ──────────────────────────────────────────────────
SERVICES = [
    "up_bot",      # 上传机器人
    "idx_bot",     # 索引机器人
    "dsp_bot",     # 投递机器人
    "mon_bot",     # 监控机器人
    "admin_bot",   # 管理机器人
    "crdb_sync",   # CRDB 同步服务
    "migration",   # 迁移工具
    "backup",      # R44 7.2: 备份服务(单独统计,不混入业务空载)
    "restore",     # R44 7.2: 恢复服务(单独统计,不混入业务空载)
]

# ─── 预算与告警阈值 ────────────────────────────────────────────
DEFAULT_DAILY_RU_LIMIT = 100_000
DEFAULT_MONTHLY_RU_LIMIT = 3_000_000
WARNING_THRESHOLD = 0.80    # 80% 告警
CRITICAL_THRESHOLD = 0.95  # 95% 严重


async def record_usage(service: str, operation: str, ru_amount: int,
                       user_id: int = 0) -> bool:
    """记录 RU 使用。

    Args:
        service: 服务名(必须在 SERVICES 列表中)
        operation: 操作类型 read/write/query/sync/migration
        ru_amount: RU 消耗量(必须 > 0)
        user_id: 触发用户(可选,记入明细)

    Returns:
        True 记录成功, False 失败
    """
    if not service or service not in SERVICES:
        logger.warning(f"[RUCostCenter] 未知服务: {service}")
        return False
    if ru_amount <= 0:
        return False

    store = get_cache_store()
    today = _dt.datetime.now().strftime("%Y%m%d")
    key = f"ru_usage:{today}:{service}"

    try:
        # 读取现有数据
        existing = await store.get_kv(key)
        if existing:
            try:
                data = json.loads(existing)
            except (json.JSONDecodeError, TypeError):
                data = {"total_ru": 0, "by_operation": {}, "events": []}
        else:
            data = {"total_ru": 0, "by_operation": {}, "events": []}

        # 累加
        data["total_ru"] = data.get("total_ru", 0) + ru_amount
        ops = data.get("by_operation", {})
        ops[operation] = ops.get(operation, 0) + ru_amount
        data["by_operation"] = ops

        # 事件明细(限制最多 100 条,避免 JSON 膨胀)
        events = data.get("events", [])
        events.append({
            "ts": _dt.datetime.now().isoformat(),
            "operation": operation,
            "ru": ru_amount,
            "user_id": user_id,
        })
        if len(events) > 100:
            events = events[-100:]
        data["events"] = events

        await store.set_kv(key, json.dumps(data, ensure_ascii=False))
        logger.debug(
            f"[RUCostCenter] 记录 RU: service={service} op={operation} "
            f"ru={ru_amount} total={data['total_ru']}"
        )
        return True
    except Exception as e:
        logger.error(f"[RUCostCenter] record_usage 失败: {e}")
        return False


async def get_daily_report(date: str | None = None) -> dict:
    """获取某日 RU 报告。

    Args:
        date: 日期字符串 YYYYMMDD,None 表示今天

    Returns:
        {date, total_ru, by_service: {service: amount}, by_operation: {op: amount}}
    """
    store = get_cache_store()
    if date is None:
        date = _dt.datetime.now().strftime("%Y%m%d")

    total_ru = 0
    by_service: dict[str, int] = {}
    by_operation: dict[str, int] = {}

    for service in SERVICES:
        key = f"ru_usage:{date}:{service}"
        try:
            raw = await store.get_kv(key)
            if not raw:
                continue
            data = json.loads(raw)
            svc_total = data.get("total_ru", 0)
            by_service[service] = svc_total
            total_ru += svc_total

            # 聚合操作类型
            ops = data.get("by_operation", {})
            for op, amount in ops.items():
                by_operation[op] = by_operation.get(op, 0) + amount
        except Exception as e:
            logger.debug(f"[RUCostCenter] 读取 {key} 失败: {e}")

    return {
        "date": date,
        "total_ru": total_ru,
        "by_service": by_service,
        "by_operation": by_operation,
    }


async def get_cost_by_service(start_date: str, end_date: str) -> dict:
    """按服务查询 RU 消耗(指定日期范围)。

    Args:
        start_date: 起始日期 YYYYMMDD
        end_date: 结束日期 YYYYMMDD

    Returns:
        {services: [{name, total_ru, daily_avg, percentage}]}
    """
    try:
        start = _dt.datetime.strptime(start_date, "%Y%m%d")
        end = _dt.datetime.strptime(end_date, "%Y%m%d")
    except ValueError as e:
        logger.error(f"[RUCostCenter] 日期格式错误: {e}")
        return {"services": []}

    if end < start:
        return {"services": []}

    # 累计每个服务在日期范围内的总 RU
    totals: dict[str, int] = {svc: 0 for svc in SERVICES}
    days_count = (end - start).days + 1

    current = start
    while current <= end:
        date_str = current.strftime("%Y%m%d")
        report = await get_daily_report(date_str)
        for svc, amount in report["by_service"].items():
            totals[svc] = totals.get(svc, 0) + amount
        current += _dt.timedelta(days=1)

    grand_total = sum(totals.values())
    services_list = []
    for svc, total in totals.items():
        services_list.append({
            "name": svc,
            "total_ru": total,
            "daily_avg": total / days_count if days_count > 0 else 0,
            "percentage": (total / grand_total * 100) if grand_total > 0 else 0,
        })
    # 按总 RU 降序
    services_list.sort(key=lambda x: x["total_ru"], reverse=True)

    return {"services": services_list}


async def get_cost_per_1k(service: str, operation: str = "upload") -> float:
    """计算每千次操作的 RU 成本。

    Args:
        service: 服务名
        operation: 操作类型(默认 upload)

    Returns:
        每千次操作的 RU 成本(基于历史数据;无数据时返回 0)
    """
    today = _dt.datetime.now().strftime("%Y%m%d")
    store = get_cache_store()
    key = f"ru_usage:{today}:{service}"

    try:
        raw = await store.get_kv(key)
        if not raw:
            return 0.0
        data = json.loads(raw)
        ops = data.get("by_operation", {})
        op_total = ops.get(operation, 0)
        events = [e for e in data.get("events", [])
                  if e.get("operation") == operation]
        event_count = len(events)
        if event_count == 0:
            return 0.0
        # 每 1000 次操作的 RU
        return (op_total / event_count) * 1000
    except Exception as e:
        logger.debug(f"[RUCostCenter] get_cost_per_1k 失败: {e}")
        return 0.0


async def get_ru_budget() -> dict:
    """获取 RU 预算(从 settings / kv_store 读取)。

    Returns:
        {daily_limit, monthly_limit, current_usage, remaining, usage_percentage}
    """
    store = get_cache_store()

    # 从 settings 读取预算(可被环境变量覆盖)
    try:
        from config import settings
        daily_limit = int(getattr(settings, "RU_DAILY_LIMIT",
                                  DEFAULT_DAILY_RU_LIMIT))
        monthly_limit = int(getattr(settings, "RU_MONTHLY_LIMIT",
                                     DEFAULT_MONTHLY_RU_LIMIT))
    except Exception:
        daily_limit = DEFAULT_DAILY_RU_LIMIT
        monthly_limit = DEFAULT_MONTHLY_RU_LIMIT

    # 当前使用量(今日)
    today = _dt.datetime.now().strftime("%Y%m%d")
    today_report = await get_daily_report(today)
    current_usage = today_report["total_ru"]

    remaining = max(0, daily_limit - current_usage)
    usage_percentage = (current_usage / daily_limit * 100) if daily_limit > 0 else 0

    return {
        "daily_limit": daily_limit,
        "monthly_limit": monthly_limit,
        "current_usage": current_usage,
        "remaining": remaining,
        "usage_percentage": round(usage_percentage, 2),
    }


async def check_ru_alert() -> dict:
    """检查 RU 告警。

    Returns:
        {alert_level: normal/warning/critical, message, threshold}
    """
    budget = await get_ru_budget()
    usage_pct = budget["usage_percentage"] / 100  # 转为 0-1

    if usage_pct >= CRITICAL_THRESHOLD:
        return {
            "alert_level": "critical",
            "message": (
                f"RU 使用率已达 {budget['usage_percentage']}% "
                f"(已用 {budget['current_usage']}/{budget['daily_limit']}),"
                f"建议立即排查异常服务或临时调高预算"
            ),
            "threshold": CRITICAL_THRESHOLD,
        }
    elif usage_pct >= WARNING_THRESHOLD:
        return {
            "alert_level": "warning",
            "message": (
                f"RU 使用率达 {budget['usage_percentage']}% "
                f"(已用 {budget['current_usage']}/{budget['daily_limit']}),"
                f"建议关注 RU 消耗趋势"
            ),
            "threshold": WARNING_THRESHOLD,
        }
    else:
        return {
            "alert_level": "normal",
            "message": (
                f"RU 使用正常 ({budget['usage_percentage']}%, "
                f"已用 {budget['current_usage']}/{budget['daily_limit']})"
            ),
            "threshold": CRITICAL_THRESHOLD,
        }


async def generate_cost_report(start_date: str, end_date: str) -> str:
    """生成成本报告(文本格式)。

    Args:
        start_date: 起始日期 YYYYMMDD
        end_date: 结束日期 YYYYMMDD

    Returns:
        多行文本报告
    """
    cost = await get_cost_by_service(start_date, end_date)
    services = cost.get("services", [])
    grand_total = sum(s["total_ru"] for s in services)

    lines: list[str] = []
    lines.append("═══════════════════════════════════════════════════════════")
    lines.append("           RU 成本报告 (R40 §9.3)")
    lines.append("═══════════════════════════════════════════════════════════")
    lines.append(f"日期范围: {start_date} ~ {end_date}")
    lines.append(f"总 RU 消耗: {grand_total}")
    lines.append("")
    lines.append(f"{'服务':<15} {'总 RU':>12} {'日均':>12} {'占比':>8}")
    lines.append("─" * 50)
    for svc in services:
        lines.append(
            f"{svc['name']:<15} {svc['total_ru']:>12} "
            f"{svc['daily_avg']:>12.1f} {svc['percentage']:>7.2f}%"
        )
    lines.append("─" * 50)

    # 预算状态
    budget = await get_ru_budget()
    alert = await check_ru_alert()
    lines.append("")
    lines.append(f"[预算] 日限额={budget['daily_limit']} "
                f"已用={budget['current_usage']} "
                f"剩余={budget['remaining']} "
                f"({budget['usage_percentage']}%)")
    lines.append(f"[告警] {alert['alert_level'].upper()}: {alert['message']}")
    lines.append("═══════════════════════════════════════════════════════════")
    return "\n".join(lines)


# ─── R44 7.2: migration/backup/restore RU 单独统计(不混入业务空载) ────

async def record_migration_usage(ru_cost: int, operation: str = "migration") -> bool:
    """R44 7.2: 记录 migration RU 消耗,单独统计不混入业务空载。

    migration 是一次性运维操作,其 RU 消耗不应计入业务角色的日常空载门禁。
    通过单独的 service='migration' 维度记录,可在 72h 空载报告中剔除。

    Args:
        ru_cost: RU 消耗量(估算值,如 DDL 语句数 * 5)
        operation: 操作类型(默认 'migration')

    Returns:
        True 记录成功, False 失败
    """
    return await record_usage("migration", operation, ru_cost)


async def record_backup_usage(ru_cost: int, operation: str = "backup") -> bool:
    """R44 7.2: 记录 backup RU 消耗,单独统计不混入业务空载。

    backup 涉及全表扫描 + R2 上传,CRDB 端 RU 消耗较大,
    但属于运维操作不应计入业务空载门禁。

    Args:
        ru_cost: RU 消耗量(估算值,默认每次 backup 约 100 RU)
        operation: 操作类型(默认 'backup')

    Returns:
        True 记录成功, False 失败
    """
    return await record_usage("backup", operation, ru_cost)


async def record_restore_usage(ru_cost: int, operation: str = "restore") -> bool:
    """R44 7.2: 记录 restore RU 消耗,单独统计不混入业务空载。

    restore 涉及批量 UPSERT,CRDB 端 RU 消耗较大,
    但属于灾备恢复操作不应计入业务空载门禁。

    Args:
        ru_cost: RU 消耗量(估算值,如恢复表数 * 50)
        operation: 操作类型(默认 'restore')

    Returns:
        True 记录成功, False 失败
    """
    return await record_usage("restore", operation, ru_cost)
