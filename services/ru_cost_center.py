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

R51 P1-7: 估算 RU vs 官方 CockroachDB Cloud Metrics 区分
    本模块存在两种 RU 数据来源:
    1. **估算值**(ru_estimated=1):
       由 record_usage() / record_migration_usage() / record_backup_usage() /
       record_restore_usage() 记录,基于 RU_PER_READ/WRITE/QUERY 常量估算。
       适用于业务侧自统计(如 Bot 内部操作计数),不依赖 CRDB Cloud API。
       缺点:无法反映真实 CRDB 负载(如网络/重试/事务回滚导致的额外 RU)。
    2. **官方值**(ru_estimated=0):
       由 record_official_usage() 记录,数据来源为 CockroachDB Cloud 官方 Metrics
       (通过 crdb_ru_collector.fetch_ru_from_crdb_cloud() 采集)。
       适用于精准成本核算与预算告警,但需要 CRDB Cloud API 凭证。
    两者不能互相替代:估算值用于业务自省,官方值用于成本核算。
    Prometheus 指标 tgjiema_ru_daily_usage{service,ru_estimated} 通过 label 区分。
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store
from services.i18n import translate as _i18n_t


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
                       user_id: int = 0,
                       ru_estimated: bool = True) -> bool:
    """记录 RU 使用。

    R51 P1-7: 新增 ``ru_estimated`` 参数区分估算值与官方值。
    - 估算值(``ru_estimated=True``,默认):基于 RU_PER_READ/WRITE/QUERY 常量估算,
      适用于业务侧自统计。
    - 官方值(``ru_estimated=False``):由 CockroachDB Cloud 官方 Metrics 采集,
      应通过 ``record_official_usage()`` 调用以确保语义清晰。

    数据格式(kv_store JSON):
        {
            "total_ru": int,
            "by_operation": {op: amount},
            "events": [...],
            "ru_estimated": int,   # R51 P1-7: 1=估算, 0=官方
        }

    Args:
        service: 服务名(必须在 SERVICES 列表中)
        operation: 操作类型 read/write/query/sync/migration
        ru_amount: RU 消耗量(必须 > 0)
        user_id: 触发用户(可选,记入明细)
        ru_estimated: R51 P1-7 是否为估算值(True=估算, False=官方)

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

        # R51 P1-7: 记录 ru_estimated 标记(1=估算, 0=官方)
        # 保守策略:一旦服务被标记为估算(ru_estimated=1),后续官方值不覆盖该标记
        # (因为估算值与官方值混合时,整体仍应视为估算,避免误信精度)
        # 新数据(无 ru_estimated 字段)按入参设置
        has_existing_estimated = "ru_estimated" in data
        if ru_estimated:
            # 估算值:强制标记为 1(无论是否已有官方值)
            data["ru_estimated"] = 1
        else:
            # 官方值:若已有估算标记(1)则保留(保守);否则设为 0
            if has_existing_estimated and data.get("ru_estimated") == 1:
                data["ru_estimated"] = 1  # 保留已有估算标记
            else:
                data["ru_estimated"] = 0  # 新数据或已有官方值 → 设为官方

        # 事件明细(限制最多 100 条,避免 JSON 膨胀)
        events = data.get("events", [])
        events.append({
            "ts": _dt.datetime.now().isoformat(),
            "operation": operation,
            "ru": ru_amount,
            "user_id": user_id,
            "ru_estimated": 1 if ru_estimated else 0,
        })
        if len(events) > 100:
            events = events[-100:]
        data["events"] = events

        await store.set_kv(key, json.dumps(data, ensure_ascii=False))
        logger.debug(
            f"[RUCostCenter] 记录 RU: service={service} op={operation} "
            f"ru={ru_amount} total={data['total_ru']} "
            f"ru_estimated={data['ru_estimated']}"
        )
        return True
    except Exception as e:
        logger.error(f"[RUCostCenter] record_usage 失败: {e}")
        return False


async def record_official_usage(service: str, ru_amount: int,
                                operation: str = "official_metric") -> bool:
    """R51 P1-7: 记录 CockroachDB Cloud 官方 Metrics RU 消耗。

    与 ``record_usage`` 的差异:
        - 数据来源:CockroachDB Cloud 官方 API(非估算)
        - ``ru_estimated=False``(标记为官方值)
        - 用于精准成本核算与预算告警

    数据来源建议:
        通过 ``services.crdb_ru_collector.fetch_ru_from_crdb_cloud()`` 采集,
        采集成功后调用本函数持久化到 kv_store。

    Args:
        service: 服务名(必须在 SERVICES 列表中)
        ru_amount: 官方 RU 消耗量(必须 > 0)
        operation: 操作类型(默认 'official_metric')

    Returns:
        True 记录成功, False 失败
    """
    if not service or service not in SERVICES:
        logger.warning(f"[RUCostCenter] record_official_usage 未知服务: {service}")
        return False
    if ru_amount <= 0:
        return False
    # 调用 record_usage 并显式标记 ru_estimated=False
    return await record_usage(
        service=service,
        operation=operation,
        ru_amount=ru_amount,
        ru_estimated=False,
    )


async def get_daily_report(date: str | None = None) -> dict:
    """获取某日 RU 报告。

    R51 P1-7: 返回结果新增 ``by_service_estimated`` 字段,标注每个服务的 RU 是否为估算值。

    Args:
        date: 日期字符串 YYYYMMDD,None 表示今天

    Returns:
        {
            date: str,
            total_ru: int,
            by_service: {service: amount},
            by_operation: {op: amount},
            by_service_estimated: {service: int},  # R51 P1-7: 1=估算, 0=官方
        }
    """
    store = get_cache_store()
    if date is None:
        date = _dt.datetime.now().strftime("%Y%m%d")

    total_ru = 0
    by_service: dict[str, int] = {}
    by_operation: dict[str, int] = {}
    # R51 P1-7: 每个服务的 ru_estimated 标记(1=估算, 0=官方)
    by_service_estimated: dict[str, int] = {}

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

            # R51 P1-7: 提取 ru_estimated 标记(默认 1=估算,兼容旧数据)
            by_service_estimated[service] = data.get("ru_estimated", 1)

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
        "by_service_estimated": by_service_estimated,
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
                _i18n_t('services.ru_cost_center.s7', budget_usage_percentage=budget['usage_percentage'], budget_current_usage=budget['current_usage'], budget_daily_limit=budget['daily_limit'])
            ),
            "threshold": CRITICAL_THRESHOLD,
        }
    elif usage_pct >= WARNING_THRESHOLD:
        return {
            "alert_level": "warning",
            "message": (
                _i18n_t('services.ru_cost_center.s8', budget_usage_percentage=budget['usage_percentage'], budget_current_usage=budget['current_usage'], budget_daily_limit=budget['daily_limit'])
            ),
            "threshold": WARNING_THRESHOLD,
        }
    else:
        return {
            "alert_level": "normal",
            "message": (
                _i18n_t('services.ru_cost_center.s9', budget_usage_percentage=budget['usage_percentage'], budget_current_usage=budget['current_usage'], budget_daily_limit=budget['daily_limit'])
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
    lines.append(_i18n_t('services.ru_cost_center.s1'))
    lines.append("═══════════════════════════════════════════════════════════")
    lines.append(_i18n_t('services.ru_cost_center.s2', start_date=start_date, end_date=end_date))
    lines.append(_i18n_t('services.ru_cost_center.s3', grand_total=grand_total))
    lines.append("")
    lines.append(_i18n_t('services.ru_cost_center.s4', p0='服务', RU='总 RU', p2='日均', p3='占比'))
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
    lines.append(_i18n_t('services.ru_cost_center.s5', budget_daily_limit=budget['daily_limit'], budget_current_usage=budget['current_usage'], budget_remaining=budget['remaining'], budget_usage_percentage=budget['usage_percentage']))
    lines.append(_i18n_t('services.ru_cost_center.s6', alert_alert_level_upper=alert['alert_level'].upper(), alert_message=alert['message']))
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


# ════════════════════════════════════════════════════════════════
#  R64 P1-10: RU 归因数据结构 + 阈值门禁(空载 RU 0/100/500 + 月度预算)
# ════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field


@dataclass
class RUAttribution:
    """R64 P1-10: RU 归因数据结构(单日聚合)。

    Attributes:
        date: 日期 YYYYMMDD
        total_ru: 当日总 RU 消耗(含所有角色)
        business_bot_ru: 业务 Bot 空载 RU(应 = 0;>0 表示有空载 CRDB 命中)
        non_business_ru: 非 Bot 角色 RU(crdb_sync/migration/backup 等)
        by_service: 按服务角色聚合 {service: amount}
        by_job: 按触发的 job 聚合 {job: amount}
        by_fingerprint: 按 SQL 指纹聚合 {fp: amount}
        by_time_bucket: 按时间桶(UTC 小时)聚合 {YYYYMMDDHH: amount}
        dau: 当日活跃用户数(用于 per-DAU 计算,可选)
    """

    date: str = ""
    total_ru: int = 0
    business_bot_ru: int = 0
    non_business_ru: int = 0
    by_service: dict[str, int] = field(default_factory=dict)
    by_job: dict[str, int] = field(default_factory=dict)
    by_fingerprint: dict[str, int] = field(default_factory=dict)
    by_time_bucket: dict[str, int] = field(default_factory=dict)
    dau: int = 0

    def per_dau_ru(self) -> float:
        """R64 P1-10: 计算 per-DAU RU(无 DAU 时返回 0)。"""
        if self.dau <= 0:
            return 0.0
        return self.total_ru / self.dau


# ── R64 P1-10: 阈值常量(与 settings 默认一致,用于 settings 缺失时回退) ──
RU_IDLE_BOT_PER_DAY_LIMIT = 0       # 业务 Bot 角色 0 RU/天(空载理想)
RU_IDLE_CLUSTER_IDEAL = 20          # 集群空载理想 ≤20 RU/天
RU_IDLE_CLUSTER_HARD_LIMIT = 100    # 集群空载硬限 ≤100 RU/天
RU_IDLE_BLOCK_THRESHOLD = 500       # >500 RU/天阻断 release
RU_PER_DAU_DAY_LIMIT = 250          # per-DAU 日均 RU 上限
RU_MONTHLY_BUDGET_LIMIT = 35_000_000  # 月度预算上限


def _get_ru_threshold_setting(name: str, default: int) -> int:
    """R64 P1-10: 从 settings 读取 RU 阈值(失败回退默认值)。"""
    try:
        from config import settings
        return int(getattr(settings, name, default))
    except Exception:
        return default


async def get_daily_attribution(date_str: str | None = None) -> RUAttribution:
    """R64 P1-10: 获取某日的 RU 归因聚合(基于 crdb_ru_collector 的归因数据)。

    本函数纯走 SQLite kv_store,零 CRDB RU。

    Args:
        date_str: 日期 YYYYMMDD,None 表示今天

    Returns:
        RUAttribution 数据结构
    """
    # 委托 crdb_ru_collector.get_ru_attribution 读取归因数据
    try:
        from services.crdb_ru_collector import get_ru_attribution
        raw = await get_ru_attribution(date_str)
    except Exception as e:
        logger.debug(f"[RUCostCenter] R64 P1-10: get_ru_attribution 失败: {e}")
        raw = {
            "date": date_str or "",
            "total_ru": 0,
            "by_service": {},
            "by_job": {},
            "by_fingerprint": {},
            "by_time_bucket": {},
            "business_bot_ru": 0,
            "non_business_ru": 0,
        }

    attr = RUAttribution(
        date=raw.get("date", date_str or ""),
        total_ru=int(raw.get("total_ru", 0)),
        business_bot_ru=int(raw.get("business_bot_ru", 0)),
        non_business_ru=int(raw.get("non_business_ru", 0)),
        by_service=dict(raw.get("by_service", {})),
        by_job=dict(raw.get("by_job", {})),
        by_fingerprint=dict(raw.get("by_fingerprint", {})),
        by_time_bucket=dict(raw.get("by_time_bucket", {})),
    )

    # 读取当日 DAU(从 kv_store.readiness_dau 或 ru_usage:YYYYMMDD:dau)
    try:
        store = get_cache_store()
        date_key = attr.date or _dt.datetime.now().strftime("%Y%m%d")
        dau_raw = await store.get_kv(f"dau:{date_key}")
        if dau_raw:
            try:
                attr.dau = int(dau_raw)
            except (TypeError, ValueError):
                attr.dau = 0
    except Exception as e:
        logger.warning(f"[RU] 获取 DAU 失败(非致命,DAU 置 0): {e}")

    return attr


async def check_daily_threshold(date_str: str | None = None) -> dict:
    """R64 P1-10: 检查当日 RU 是否超过告警/阻断阈值。

    阈值(从 settings 读取,默认值见常量):
        - 业务 Bot 空载 RU:必须 = 0(任何 >0 视为门禁违规)
        - 集群空载 RU 硬限:≤100 RU/天(>100 告警)
        - 阻断 release 阈值:>500 RU/天
        - per-DAU 限制:≤250 RU/DAU/天

    Args:
        date_str: 日期 YYYYMMDD,None 表示今天

    Returns:
        {
            "date": str,
            "passed": bool,           # True=所有门禁通过, False=有违规
            "block_release": bool,    # True=阻断 release(>500 RU 或 Bot RU>0)
            "alert": bool,            # True=告警(>100 RU)
            "violations": [str],      # 违规描述列表
            "business_bot_ru": int,
            "total_ru": int,
            "per_dau_ru": float,
            "thresholds": {...},      # 使用的阈值
        }
    """
    attr = await get_daily_attribution(date_str)
    alert_threshold = _get_ru_threshold_setting(
        "CRDB_RU_DAILY_ALERT_THRESHOLD", RU_IDLE_CLUSTER_HARD_LIMIT
    )
    block_threshold = _get_ru_threshold_setting(
        "CRDB_RU_DAILY_BLOCK_THRESHOLD", RU_IDLE_BLOCK_THRESHOLD
    )
    per_dau_limit = _get_ru_threshold_setting(
        "CRDB_RU_DAU_DAY_LIMIT", RU_PER_DAU_DAY_LIMIT
    )

    violations: list[str] = []
    block_release = False
    alert = False

    # 1. 业务 Bot 空载 RU 必须为 0
    if attr.business_bot_ru > 0:
        violations.append(
            f"业务 Bot 空载 RU > 0: {attr.business_bot_ru} RU "
            f"(要求 0 RU/天)"
        )
        # 业务 Bot 任何空载 RU 均视为阻断(违反"72 小时 0 RU"要求)
        block_release = True
        alert = True

    # 2. 集群空载 RU 硬限
    if attr.total_ru > block_threshold:
        violations.append(
            f"集群日 RU {attr.total_ru} > 阻断阈值 {block_threshold} RU"
        )
        block_release = True
        alert = True
    elif attr.total_ru > alert_threshold:
        violations.append(
            f"集群日 RU {attr.total_ru} > 告警阈值 {alert_threshold} RU"
        )
        alert = True

    # 3. per-DAU 限制(仅在有 DAU 数据时检查)
    if attr.dau > 0:
        per_dau = attr.per_dau_ru()
        if per_dau > per_dau_limit:
            violations.append(
                f"per-DAU RU {per_dau:.1f} > 限制 {per_dau_limit} RU/DAU/天"
            )
            alert = True
            # per-DAU 超限不阻断 release(只有 Bot 空载 / 集群硬限阻断)

    return {
        "date": attr.date,
        "passed": len(violations) == 0,
        "block_release": block_release,
        "alert": alert,
        "violations": violations,
        "business_bot_ru": attr.business_bot_ru,
        "total_ru": attr.total_ru,
        "non_business_ru": attr.non_business_ru,
        "dau": attr.dau,
        "per_dau_ru": attr.per_dau_ru() if attr.dau > 0 else 0.0,
        "thresholds": {
            "alert": alert_threshold,
            "block": block_threshold,
            "per_dau_limit": per_dau_limit,
            "bot_idle_limit": RU_IDLE_BOT_PER_DAY_LIMIT,
        },
    }


async def check_monthly_budget(year_month: str | None = None) -> dict:
    """R64 P1-10: 检查月度 RU 预算(累计当月所有日 RU)。

    阈值:
        - 月度预算:35,000,000 RU(从 settings.CRDB_RU_MONTHLY_BUDGET 读取)
        - 超过预算:阻断 release

    Args:
        year_month: 月份 YYYYMM,None 表示当月

    Returns:
        {
            "year_month": str,
            "passed": bool,
            "block_release": bool,
            "monthly_usage": int,
            "monthly_budget": int,
            "remaining": int,
            "usage_percentage": float,
        }
    """
    if year_month is None:
        year_month = _dt.datetime.now().strftime("%Y%m")

    # 解析月份
    try:
        ym_date = _dt.datetime.strptime(year_month, "%Y%m")
    except ValueError:
        return {
            "year_month": year_month,
            "passed": False,
            "block_release": True,
            "monthly_usage": 0,
            "monthly_budget": _get_ru_threshold_setting(
                "CRDB_RU_MONTHLY_BUDGET", RU_MONTHLY_BUDGET_LIMIT
            ),
            "remaining": 0,
            "usage_percentage": 0.0,
            "error": "year_month 格式错误(应为 YYYYMM)",
        }

    # 累计当月所有日 RU(从 1 日到月末)
    if ym_date.month == 12:
        next_month = ym_date.replace(year=ym_date.year + 1, month=1)
    else:
        next_month = ym_date.replace(month=ym_date.month + 1)
    days_in_month = (next_month - ym_date).days

    monthly_usage = 0
    for day in range(1, days_in_month + 1):
        date_str = f"{year_month}{day:02d}"
        report = await get_daily_report(date_str)
        monthly_usage += report.get("total_ru", 0)

    monthly_budget = _get_ru_threshold_setting(
        "CRDB_RU_MONTHLY_BUDGET", RU_MONTHLY_BUDGET_LIMIT
    )
    remaining = max(0, monthly_budget - monthly_usage)
    usage_pct = (
        (monthly_usage / monthly_budget * 100) if monthly_budget > 0 else 0.0
    )
    block_release = monthly_usage > monthly_budget

    return {
        "year_month": year_month,
        "passed": not block_release,
        "block_release": block_release,
        "monthly_usage": monthly_usage,
        "monthly_budget": monthly_budget,
        "remaining": remaining,
        "usage_percentage": round(usage_pct, 2),
    }


def get_idle_crdb_audit_summary() -> dict:
    """R64 P1-10: 静态审计摘要 — 列出已确认无空载 CRDB 命中的服务。

    本函数用于 CI 门禁与测试断言,确认以下服务已迁移到 SQLite/Redis 事件驱动:
        - r40_scheduler:周期任务全走 SQLite(cache_store / command_bus)
        - crdb_sync_service:leader 走 Redis SET NX;dirty 检测走 SQLite;
                            CRDB 仅 dirty 驱动懒加载,无空载轮询
        - prometheus_exporter:所有指标走 SQLite;/health & /readiness 走 SQLite ping

    Returns:
        {
            "audited_services": list[str],
            "policy": str,
            "allowed_crdb_triggers": list[str],
        }
    """
    try:
        from services.crdb_ru_collector import IDLE_CRDB_FREE_SERVICES_AUDITED
    except Exception:
        IDLE_CRDB_FREE_SERVICES_AUDITED = frozenset()  # noqa: F811

    return {
        "audited_services": sorted(IDLE_CRDB_FREE_SERVICES_AUDITED),
        "policy": (
            "业务 Bot 角色不得产生空载 CRDB RU;周期任务必须走 SQLite/Redis/"
            "事件驱动;CRDB 触达仅由 dirty/event 显式驱动"
        ),
        "allowed_crdb_triggers": [
            "dirty_outbox event(drdb_sync 懒加载)",
            "user-initiated operation(db_writer)",
            "explicit oneshot(migration / bootstrap / disaster_recovery)",
            "explicit backup/restore(manual trigger)",
        ],
    }
