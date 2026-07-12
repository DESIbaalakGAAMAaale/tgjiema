"""R40 §9.3: 拓扑可视化 — 副本因子/频道健康/FloodWait/账号风险/R100延迟。

职责:
    提供拓扑结构可视化与风险评估,包括:
    1. 完整拓扑结构(账号 → 频道映射)
    2. 频道健康状态(从 cells_local + heartbeat_local 表读取)
    3. 账号风险评估(flood_wait_count / restrictions)
    4. R100 兜底频道延迟(从 local_job_queue 推断)
    5. 副本因子状态(target/current/healthy/degraded/missing)

设计原则:
    - 纯函数式 + async
    - 通过 database.cache_store.get_cache_store() 获取单例
    - 从 cells_local + heartbeat_local + bot_heartbeat + relay_db 聚合数据
    - 不修改任何状态,只读
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store


# ─── 副本因子目标值(可被 settings 覆盖) ────────────────────────
DEFAULT_TARGET_REPLICATION_FACTOR = 3


async def get_topology() -> dict:
    """获取完整拓扑结构。

    Returns:
        {
            accounts: [{id, name, phone, status, channels: [...], is_active}],
            channels: [{slot_id, channel_id, account_name, role, health,
                        file_count, status, last_heartbeat}],
            replication_factor: {current, target, healthy},
        }
    """
    store = get_cache_store()

    # 1. 读取所有 cells_local(频道环形结构)
    cells = []
    if store._db:
        try:
            rows = await store._db.execute_fetchall(
                """SELECT slot_id, channel_id, status, next_active_chat_id,
                          prev_slot_id, account_name, is_r100,
                          last_heartbeat, last_synced_msg_id,
                          degrade_count, file_count, updated_at
                   FROM cells_local
                   WHERE deleted_at IS NULL OR deleted_at = ''
                   ORDER BY slot_id"""
            )
            for r in rows:
                cells.append({
                    "slot_id": r[0],
                    "channel_id": r[1],
                    "status": r[2],
                    "next_active_chat_id": r[3],
                    "prev_slot_id": r[4],
                    "account_name": r[5] or "",
                    "is_r100": bool(r[6]),
                    "last_heartbeat": r[7],
                    "last_synced_msg_id": r[8],
                    "degrade_count": r[9],
                    "file_count": r[10],
                    "updated_at": r[11],
                })
        except Exception as e:
            logger.error(f"[TopologyView] 读取 cells_local 失败: {e}")

    # 2. 读取 relay 账号
    accounts: list[dict] = []
    try:
        from database.relay_db import get_relay_db
        relay_db = await get_relay_db()
        acc_rows = await relay_db._db.execute_fetchall(
            """SELECT id, phone, status, status_info, is_active,
                      status_updated_at, last_login_at
               FROM relay_accounts
               WHERE deleted_at IS NULL OR deleted_at = ''
               ORDER BY id"""
        )
        for r in acc_rows:
            accounts.append({
                "id": r[0], "name": f"relay-{r[0]}", "phone": r[1],
                "status": r[2] or "unknown", "status_info": r[3] or "",
                "is_active": bool(r[4]),
                "status_updated_at": r[5], "last_login_at": r[6],
                "channels": [],
            })
    except Exception as e:
        logger.debug(f"[TopologyView] 读取 relay_accounts 失败: {e}")

    # 3. 关联账号 ↔ 频道(基于 account_name 字段)
    # 将 cells 按 account_name 分组,但 account_name 可能不是手机号
    # 简化处理:把所有 cells 列表挂在每个账号上(运维可肉眼检查关联)
    for acc in accounts:
        # 匹配 account_name == phone 或 status 中提及的频道
        acc_channels = [
            {
                "slot_id": c["slot_id"],
                "channel_id": c["channel_id"],
                "status": c["status"],
                "is_r100": c["is_r100"],
                "file_count": c["file_count"],
                "last_heartbeat": c["last_heartbeat"],
            }
            for c in cells
        ]
        acc["channels"] = acc_channels

    # 4. 副本因子统计
    target_factor = _get_target_replication_factor()
    active_cells = [c for c in cells if c["status"] == "active"]
    healthy_cells = [c for c in active_cells if not c["is_r100"]]
    current_factor = len(healthy_cells)
    replication_factor = {
        "current": current_factor,
        "target": target_factor,
        "healthy": current_factor >= target_factor,
    }

    return {
        "accounts": accounts,
        "channels": cells,
        "replication_factor": replication_factor,
    }


async def get_channel_health() -> list[dict]:
    """获取频道健康状态列表。

    从 cells_local + heartbeat_local 表读取。

    Returns:
        [{channel_id, slot_id, status, last_heartbeat, file_count,
          error_count, fail_streak, is_r100, health}]
        health: healthy/degraded/down
    """
    store = get_cache_store()
    if not store._db:
        return []

    try:
        rows = await store._db.execute_fetchall(
            """SELECT c.slot_id, c.channel_id, c.status, c.last_heartbeat,
                      c.file_count, c.degrade_count, c.is_r100,
                      COALESCE(h.fail_streak, 0) as fail_streak,
                      COALESCE(h.last_ok, 0) as last_ok
               FROM cells_local c
               LEFT JOIN heartbeat_local h ON c.slot_id = h.slot_id
               WHERE c.deleted_at IS NULL OR c.deleted_at = ''
               ORDER BY c.slot_id"""
        )

        result = []
        now_ts = _dt.datetime.now().timestamp()
        for r in rows:
            slot_id = r[0]
            channel_id = r[1]
            status = r[2]
            last_heartbeat = r[3]
            file_count = r[4] or 0
            degrade_count = r[5] or 0
            is_r100 = bool(r[6])
            fail_streak = r[7] or 0
            last_ok = r[8] or 0

            # 判定健康状态:fail_streak >= 3 视为 down,
            # degrade_count > 0 或 fail_streak >= 1 视为 degraded,否则 healthy
            if fail_streak >= 3:
                health = "down"
            elif degrade_count > 0 or fail_streak >= 1:
                health = "degraded"
            else:
                health = "healthy"

            result.append({
                "channel_id": channel_id,
                "slot_id": slot_id,
                "status": status,
                "last_heartbeat": last_heartbeat,
                "file_count": file_count,
                "error_count": degrade_count,
                "fail_streak": fail_streak,
                "last_ok": last_ok,
                "is_r100": is_r100,
                "health": health,
            })
        return result
    except Exception as e:
        logger.error(f"[TopologyView] get_channel_health 失败: {e}")
        return []


async def get_account_risk() -> list[dict]:
    """获取账号风险评估。

    Returns:
        [{account_id, name, phone, risk_level, flood_wait_count,
          last_activity, restrictions, status}]
        risk_level: low/medium/high/critical
    """
    try:
        from database.relay_db import get_relay_db
        relay_db = await get_relay_db()

        rows = await relay_db._db.execute_fetchall(
            """SELECT id, phone, status, status_info, is_active,
                      status_updated_at, last_login_at
               FROM relay_accounts
               WHERE deleted_at IS NULL OR deleted_at = ''
               ORDER BY id"""
        )

        result = []
        for r in rows:
            account_id = r[0]
            phone = r[1]
            status = r[2] or "unknown"
            status_info = r[3] or ""
            is_active = bool(r[4])
            status_updated_at = r[5]
            last_login_at = r[6]

            # 风险评估逻辑
            risk_level = "low"
            flood_wait_count = 0
            restrictions: list[str] = []

            if status == "banned":
                risk_level = "critical"
                restrictions.append("banned")
            elif status == "restricted":
                risk_level = "high"
                restrictions.append("restricted")
            elif status == "flood_wait":
                risk_level = "high"
                flood_wait_count = 1
                # 尝试从 status_info 解析等待秒数
                if status_info:
                    try:
                        flood_wait_count = int(status_info.split()[0]) if status_info.split() else 1
                    except (ValueError, IndexError):
                        flood_wait_count = 1
            elif status == "unknown" and not is_active:
                risk_level = "medium"
                restrictions.append("inactive")
            elif status == "unknown":
                risk_level = "low"
            elif status in ("ok", "active"):
                risk_level = "low"
            else:
                risk_level = "medium"

            result.append({
                "account_id": account_id,
                "name": f"relay-{account_id}",
                "phone": phone,
                "risk_level": risk_level,
                "flood_wait_count": flood_wait_count,
                "last_activity": status_updated_at or last_login_at,
                "restrictions": restrictions,
                "status": status,
                "is_active": is_active,
            })
        return result
    except Exception as e:
        logger.error(f"[TopologyView] get_account_risk 失败: {e}")
        return []


async def get_r100_delay() -> dict:
    """获取 R100 副本延迟。

    R100 是兜底频道(不参与环形调度),其延迟可通过 local_job_queue 中
    状态为 'pending' 的任务数推断。

    Returns:
        {delay_seconds, pending_count, estimated_completion}
    """
    store = get_cache_store()
    if not store._db:
        return {
            "delay_seconds": 0, "pending_count": 0,
            "estimated_completion": 0,
        }

    try:
        # 统计 pending 任务数
        rows = await store._db.execute_fetchall(
            "SELECT COUNT(*), MIN(created_at) FROM local_job_queue WHERE status = 'pending'"
        )
        if rows and rows[0]:
            pending_count = rows[0][0] or 0
            oldest_created = rows[0][1]
        else:
            pending_count = 0
            oldest_created = None

        # 延迟 = 当前时间 - 最早 pending 任务创建时间
        delay_seconds = 0
        if oldest_created and isinstance(oldest_created, (int, float)):
            delay_seconds = max(0, _dt.datetime.now().timestamp() - oldest_created)
        elif oldest_created and isinstance(oldest_created, str):
            try:
                created_dt = _dt.datetime.fromisoformat(oldest_created)
                delay_seconds = max(
                    0, (_dt.datetime.now() - created_dt).total_seconds()
                )
            except (ValueError, TypeError):
                delay_seconds = 0

        # 估算完成时间:假设每秒处理 1 个任务
        estimated_completion = pending_count * 1.0

        return {
            "delay_seconds": int(delay_seconds),
            "pending_count": pending_count,
            "estimated_completion": int(estimated_completion),
        }
    except Exception as e:
        logger.error(f"[TopologyView] get_r100_delay 失败: {e}")
        return {
            "delay_seconds": 0, "pending_count": 0,
            "estimated_completion": 0,
        }


async def get_replica_status() -> dict:
    """获取副本因子状态。

    Returns:
        {target_factor, current_factor, healthy_channels,
         degraded_channels, missing_replicas}
    """
    target = _get_target_replication_factor()
    channels_health = await get_channel_health()

    # 仅统计非 R100 频道
    non_r100 = [c for c in channels_health if not c.get("is_r100", False)]
    healthy_channels = sum(1 for c in non_r100 if c["health"] == "healthy")
    degraded_channels = sum(1 for c in non_r100 if c["health"] == "degraded")
    down_channels = sum(1 for c in non_r100 if c["health"] == "down")

    # 副本因子 = 健康 + 降级(降级仍可服务)
    current_factor = healthy_channels + degraded_channels
    missing_replicas = max(0, target - current_factor)

    return {
        "target_factor": target,
        "current_factor": current_factor,
        "healthy_channels": healthy_channels,
        "degraded_channels": degraded_channels,
        "missing_replicas": missing_replicas,
        "down_channels": down_channels,
    }


async def format_topology(topology: dict) -> str:
    """格式化拓扑为管理员可读文本(ASCII 树形图)。

    Args:
        topology: get_topology() 返回的字典

    Returns:
        ASCII 格式的拓扑文本
    """
    lines: list[str] = []
    lines.append("═══════════════════════════════════════════════════════════")
    lines.append("                  拓扑可视化 (R40 §9.3)")
    lines.append("═══════════════════════════════════════════════════════════")

    # 副本因子
    rf = topology.get("replication_factor", {})
    lines.append(
        f"\n[副本因子] current={rf.get('current', 0)} "
        f"target={rf.get('target', 0)} "
        f"healthy={'YES' if rf.get('healthy') else 'NO'}"
    )

    # 账号 → 频道
    accounts = topology.get("accounts", [])
    lines.append(f"\n[账号] 共 {len(accounts)} 个")
    for acc in accounts:
        status_mark = "✓" if acc.get("is_active") else "✗"
        lines.append(
            f"  {status_mark} #{acc.get('id')} {acc.get('phone')} "
            f"[{acc.get('status', 'unknown')}] "
            f"channels={len(acc.get('channels', []))}"
        )

    # 频道列表
    channels = topology.get("channels", [])
    lines.append(f"\n[频道] 共 {len(channels)} 个")
    for ch in channels:
        r100_mark = " [R100]" if ch.get("is_r100") else ""
        lines.append(
            f"  • slot={ch.get('slot_id')} ch={ch.get('channel_id')} "
            f"status={ch.get('status')} files={ch.get('file_count', 0)}"
            f"{r100_mark}"
        )

    lines.append("\n═══════════════════════════════════════════════════════════")
    return "\n".join(lines)


async def get_health_summary() -> dict:
    """获取健康摘要(用于 Admin Dashboard)。

    Returns:
        {overall_status, healthy_count, warning_count, critical_count,
         last_updated}
        overall_status: healthy/degraded/critical
    """
    channels_health = await get_channel_health()
    accounts_risk = await get_account_risk()
    replica_status = await get_replica_status()

    healthy_count = 0
    warning_count = 0
    critical_count = 0

    # 频道健康统计
    for ch in channels_health:
        if ch["health"] == "healthy":
            healthy_count += 1
        elif ch["health"] == "degraded":
            warning_count += 1
        else:  # down
            critical_count += 1

    # 账号风险统计
    for acc in accounts_risk:
        risk = acc.get("risk_level", "low")
        if risk == "low":
            healthy_count += 1
        elif risk == "medium":
            warning_count += 1
        else:  # high/critical
            critical_count += 1

    # 综合状态判定
    if critical_count > 0:
        overall_status = "critical"
    elif warning_count > 0:
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    # 副本因子不足也视为降级
    if replica_status.get("missing_replicas", 0) > 0 and overall_status == "healthy":
        overall_status = "degraded"

    return {
        "overall_status": overall_status,
        "healthy_count": healthy_count,
        "warning_count": warning_count,
        "critical_count": critical_count,
        "missing_replicas": replica_status.get("missing_replicas", 0),
        "last_updated": _dt.datetime.now().isoformat(),
    }


# ─── 内部辅助 ──────────────────────────────────────────────────

def _get_target_replication_factor() -> int:
    """从 settings 读取目标副本因子,默认 3。"""
    try:
        from config import settings
        return int(getattr(settings, "TARGET_REPLICATION_FACTOR",
                          DEFAULT_TARGET_REPLICATION_FACTOR))
    except Exception:
        return DEFAULT_TARGET_REPLICATION_FACTOR
