"""R37 P1-8: 引导执行器(bootstrap_runner) — 显式人工或恢复任务,限速且可观测。

职责边界(P1-8 拆分):
  - migration_runner: 唯一允许 DDL/TTL/版本写入
  - bootstrap_runner: 显式人工或恢复任务,限速且可观测(此模块)
  - runtime_client(database.session.init_db): 只连接/查询,不导入 DDL,不 bootstrap

职责:
  - 预填充 cells 快照到 SQLite(避免 Mon Bot 首次运行回退到 CRDB)
  - 全表缓存热路径到 SQLite(users / codes / file_records / external_code_mapping)
  - 启动时从 CRDB 全量加载,之后所有读操作走 SQLite(0 CRDB RU)

R45 §7.1 新增:
  - bootstrap_admin_principal_atomic() — 原子 bootstrap 管理员身份
    (principal 创建 + role 分配 + 审计日志 在同一 SQLite 事务中)

调用方:
  - systemd bootstrap oneshot 服务(可选,在 migration 之后、业务 Bot 之前)
  - docker-compose bootstrap 服务(可选,depends_on migration)
  - 严禁业务 Bot / crdb_sync / db_writer 调用此模块

设计原则:
  - 显式性: 必须由独立服务调用,不在 runtime_client 中自动触发
  - 限速: 每张表全量加载,控制对 CRDB 的并发查询压力
  - 可观测: 每张表加载行数 + 耗时记录到日志
  - 幂等性: 重复执行不会产生副作用(SQLite 已有数据时跳过)
"""
from __future__ import annotations

import asyncio
import datetime
import json
import time
from typing import Any

from loguru import logger


async def run_bootstrap(
    *,
    force: bool = False,
    tables: list[str] | None = None,
) -> dict[str, Any]:
    """R37 P1-8: 执行 SQLite 全表缓存 bootstrap(显式人工或恢复任务)。

    本函数:
      1. 预填充 cells 快照到 SQLite(避免 Mon Bot 回退到 CRDB)
      2. 全表加载 users / codes / file_records / external_code_mapping

    Args:
        force: True 强制重新加载(忽略已有数据);False 幂等跳过已有数据
        tables: 指定加载的表名列表(None 加载全部)

    Returns:
        {
            "loaded_tables": int,        # 实际加载的表数
            "total_rows": int,           # 加载总行数
            "table_stats": {table: {rows, elapsed_ms}},
            "errors": [str],             # 失败错误列表
        }
    """
    from database.cache_store import get_cache_store
    from database.session import (
        D1Collection, get_cells_col, get_users_col,
        get_file_records_col, get_codes_col, get_external_code_mapping_col,
    )
    from config import settings as _settings
    from database.session import CockroachDBClient

    logger.info(
        f"[bootstrap_runner] R37 P1-8: 开始 SQLite 全表缓存 bootstrap "
        f"(role={_settings.SERVICE_ROLE}, force={force}, tables={tables})"
    )

    result: dict[str, Any] = {
        "loaded_tables": 0,
        "total_rows": 0,
        "table_stats": {},
        "errors": [],
    }

    # 1. 初始化 SQLite 缓存
    store = get_cache_store()
    await store.init()

    # 2. 创建独立 CRDB 连接(只读,不污染 runtime pool)
    client = CockroachDBClient()
    client.configure(_settings.COCKROACHDB_URL)
    await client.connect_runtime_only()

    try:
        # 3. cells 预填充
        if tables is None or "cells" in tables:
            t0 = time.monotonic()
            try:
                existing_cells = await store.get_all_cells_local()
                if existing_cells and not force:
                    logger.info(
                        f"[bootstrap_runner] cells 已存在({len(existing_cells)} 条),跳过"
                    )
                    result["table_stats"]["cells"] = {
                        "rows": len(existing_cells), "elapsed_ms": 0, "skipped": True,
                    }
                else:
                    # 先尝试从 SQLite snapshot 加载(0 CRDB RU)
                    snap_cells, _ = await store.load_cells_snapshot()
                    if snap_cells and not force:
                        await store.bulk_upsert_cells_local(snap_cells)
                        result["table_stats"]["cells"] = {
                            "rows": len(snap_cells),
                            "elapsed_ms": int((time.monotonic() - t0) * 1000),
                        }
                        result["total_rows"] += len(snap_cells)
                        result["loaded_tables"] += 1
                        logger.info(
                            f"[bootstrap_runner] cells 从 snapshot 加载: "
                            f"{len(snap_cells)} 条 ({result['table_stats']['cells']['elapsed_ms']}ms)"
                        )
                    else:
                        # 从 CRDB 全量加载
                        col = get_cells_col()
                        all_cells = await col.find({}, projection=[
                            "slot_id", "channel_id", "status", "next_active_chat_id",
                            "prev_slot_id", "account_name", "is_r100",
                            "file_count", "rotation_started_at", "last_heartbeat",
                        ])
                        if all_cells:
                            await store.bulk_upsert_cells_local(all_cells)
                            result["table_stats"]["cells"] = {
                                "rows": len(all_cells),
                                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                            }
                            result["total_rows"] += len(all_cells)
                            result["loaded_tables"] += 1
                            logger.info(
                                f"[bootstrap_runner] cells 从 CRDB 加载: "
                                f"{len(all_cells)} 条 ({result['table_stats']['cells']['elapsed_ms']}ms)"
                            )
            except Exception as e:
                result["errors"].append(f"cells: {e}")
                logger.warning(f"[bootstrap_runner] cells 预填充失败: {e}")

        # 4. 全表缓存热路径: file_records / codes / users / external_code_mapping
        table_configs = [
            ("file_records", "file_records_local", get_file_records_col,
             ["file_code", "uploader_id", "primary_channel_id", "primary_channel_msg_id",
              "file_types", "backup_channel_msg_ids", "batch_msg_ids", "batch_file_meta",
              "file_ids", "status", "request_count", "protect_content", "file_ttl_days",
              "note", "expire_time", "blocked_users", "create_time", "updated_at",
              "max_requests", "is_collection", "collection_codes"],
             store.bootstrap_file_records),
            ("codes", "codes_local", get_codes_col,
             ["code", "file_record_code", "uploader_id", "file_types",
              "batch_msg_ids", "batch_file_meta", "primary_channel_id",
              "status", "created_at", "expire_time", "note"],
             store.bootstrap_codes),
            ("users", "users_local", get_users_col,
             ["user_id", "username", "first_name", "membership_level",
              "daily_decode_quota", "quota_used_today", "quota_date",
              "can_upload", "external_decode_quota", "external_used_today",
              "external_quota_date", "is_banned", "created_at", "updated_at"],
             store.bootstrap_users),
            ("external_code_mapping", "external_code_mapping_local",
             get_external_code_mapping_col,
             ["external_code", "system_code", "bot_username", "created_at", "updated_at"],
             store.bootstrap_external_mappings),
        ]

        for table_name, sqlite_table, col_getter, projection, bootstrap_fn in table_configs:
            if tables is not None and table_name not in tables:
                continue
            t0 = time.monotonic()
            try:
                # 幂等检查:已有数据则跳过(force=False 时)
                if not force:
                    count_rows = await store._db.execute_fetchall(
                        f"SELECT COUNT(*) FROM {sqlite_table}"
                    )
                    existing_count = count_rows[0][0] if count_rows else 0
                    if existing_count > 0:
                        logger.info(
                            f"[bootstrap_runner] {table_name} 已有 {existing_count} 条,跳过"
                        )
                        result["table_stats"][table_name] = {
                            "rows": existing_count, "elapsed_ms": 0, "skipped": True,
                        }
                        continue

                # 从 CRDB 全量加载
                col = col_getter()
                rows = await col.find({}, projection=projection)
                if rows:
                    await bootstrap_fn(rows)
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    result["table_stats"][table_name] = {
                        "rows": len(rows), "elapsed_ms": elapsed_ms,
                    }
                    result["total_rows"] += len(rows)
                    result["loaded_tables"] += 1
                    logger.info(
                        f"[bootstrap_runner] {table_name} 从 CRDB 加载: "
                        f"{len(rows)} 条 ({elapsed_ms}ms)"
                    )
                else:
                    result["table_stats"][table_name] = {
                        "rows": 0, "elapsed_ms": 0, "empty": True,
                    }
                    logger.info(f"[bootstrap_runner] {table_name} CRDB 无数据")
            except Exception as e:
                result["errors"].append(f"{table_name}: {e}")
                logger.warning(f"[bootstrap_runner] {table_name} 预填充失败: {e}")

        logger.info(
            f"[bootstrap_runner] R37 P1-8: bootstrap 完成 "
            f"(加载表 {result['loaded_tables']}, 总行数 {result['total_rows']}, "
            f"错误 {len(result['errors'])})"
        )
        return result

    finally:
        await client.close()


# ─── R45 §7.1: 原子 bootstrap 管理员身份 ─────────────────────────


async def bootstrap_admin_principal_atomic(
    principal_id: int | None = None,
    username: str | None = None,
    roles: list[str] | None = None,
    source: str = "bootstrap_runner",
) -> dict[str, Any]:
    """R45 §7.1: 原子 bootstrap 管理员 principal(单事务 + 幂等 + 审计)。

    将以下操作封装为单一 SQLite 事务(由 CacheStore.bootstrap_admin_principal 实现,
    任一步骤失败则回滚,确保不会出现"principal 已创建但角色未分配"的部分状态):
      1. UPSERT admin_principals 记录(id, username, is_active=1)
      2. 清除旧 admin_principal_roles 映射(幂等覆盖)
      3. 插入新角色映射(默认 super_admin)
      4. 写 audit_log(action=bootstrap_admin_principal, 记录 bootstrap 时间/
         principal_id/role/source)

    幂等性:
      - 若 principal 已存在且角色一致 → 直接返回成功(skipped=True)
      - 若 principal 不存在或角色不一致 → 执行 bootstrap(skipped=False)

    Args:
        principal_id: 管理员主体 ID(>0);None 时从 settings.ADMIN_PRINCIPAL_ID 读取
        username: 管理员用户名;None 时从 settings 读取
        roles: 角色列表;None 时从 settings.ADMIN_PRINCIPAL_BOOTSTRAP_ROLES 解析
        source: bootstrap 来源标记(如 "bootstrap_runner" / "manual" / "systemd"),
                记录到 audit_log details 中便于审计追踪

    Returns:
        {
            "success": bool,           # 是否成功
            "skipped": bool,           # 是否因幂等跳过(已 bootstrap)
            "principal_id": int,       # bootstrap 的 principal ID
            "username": str,           # bootstrap 的用户名
            "roles": list[str],        # 分配的角色
            "source": str,             # bootstrap 来源
            "bootstrap_time": str,     # bootstrap 时间(ISO)
            "error": str,              # 失败时的错误信息(成功为空)
        }
    """
    from config import settings as _settings
    from database.cache_store import get_cache_store

    result: dict[str, Any] = {
        "success": False,
        "skipped": False,
        "principal_id": 0,
        "username": "",
        "roles": [],
        "source": source,
        "bootstrap_time": datetime.datetime.now().isoformat(),
        "error": "",
    }

    # 1. 解析 principal_id
    if principal_id is None:
        try:
            principal_id = int(getattr(_settings, "ADMIN_PRINCIPAL_ID", 0) or 0)
        except (TypeError, ValueError):
            principal_id = 0
    if not principal_id or principal_id <= 0:
        result["error"] = "principal_id 未配置(ADMIN_PRINCIPAL_ID<=0)"
        logger.error(f"[bootstrap_admin_principal_atomic] {result['error']}")
        return result
    result["principal_id"] = principal_id

    # 2. 解析 username
    if username is None:
        username = getattr(_settings, "ADMIN_PRINCIPAL_USERNAME", "") or ""
    if not username:
        username = getattr(_settings, "ADMIN_USERNAME", "") or ""
    if not username:
        result["error"] = "username 未配置"
        logger.error(f"[bootstrap_admin_principal_atomic] {result['error']}")
        return result
    result["username"] = username

    # 3. 解析 roles
    if roles is None:
        raw_roles = getattr(_settings, "ADMIN_PRINCIPAL_BOOTSTRAP_ROLES", "") or ""
        if raw_roles:
            roles = [r.strip() for r in raw_roles.split(",") if r.strip()]
        else:
            roles = ["super_admin"]
    if not roles:
        roles = ["super_admin"]
    result["roles"] = roles

    # 4. 初始化 CacheStore
    store = get_cache_store()
    if not store._db:
        await store.init()
    if not store._db:
        result["error"] = "CacheStore DB 未初始化"
        logger.error(f"[bootstrap_admin_principal_atomic] {result['error']}")
        return result

    # 5. 幂等检查 — 若 principal 已存在且角色一致,跳过
    try:
        existing = await store.get_admin_principal_record(principal_id)
        if existing is not None and existing.get("is_active", False):
            existing_roles = await store.list_admin_principal_roles(principal_id)
            if existing_roles == roles:
                logger.info(
                    f"[bootstrap_admin_principal_atomic] principal={principal_id} "
                    f"已 bootstrap 且角色一致,跳过(幂等)"
                )
                result["success"] = True
                result["skipped"] = True
                return result
    except Exception as e:
        logger.warning(
            f"[bootstrap_admin_principal_atomic] 幂等检查失败(继续执行 bootstrap): {e}"
        )

    # 6. 执行原子 bootstrap(委托 CacheStore.bootstrap_admin_principal)
    # 该方法在单 transaction 中完成 UPSERT + 角色分配 + audit_log,失败回滚
    try:
        ok = await store.bootstrap_admin_principal(
            principal_id=principal_id,
            username=username,
            roles=roles,
        )
        if not ok:
            result["error"] = "CacheStore.bootstrap_admin_principal 返回 False"
            logger.error(
                f"[bootstrap_admin_principal_atomic] bootstrap 失败 "
                f"principal={principal_id} user={username}"
            )
            return result
    except Exception as e:
        result["error"] = f"bootstrap 异常: {e}"
        logger.error(
            f"[bootstrap_admin_principal_atomic] bootstrap 异常 "
            f"principal={principal_id} user={username}: {e}"
        )
        return result

    # 7. 补充审计日志(记录 source / bootstrap_time,与 cache_store 的 audit_log 互补)
    # cache_store.bootstrap_admin_principal 已写 audit_log(action=bootstrap_admin_principal),
    # 此处额外写一条记录 source 和 bootstrap_time 的详细审计(便于运维排查)
    try:
        now = datetime.datetime.now().isoformat()
        await store._db.execute(
            "INSERT INTO audit_log (actor_id, actor_type, action, target_type, "
            "target_id, details, ip_addr, created_at) "
            "VALUES (?, 'system', 'bootstrap_admin_principal_atomic', 'admin_principal', ?, ?, '', ?)",
            (
                0,
                str(principal_id),
                json.dumps({
                    "principal_id": principal_id,
                    "username": username,
                    "roles": roles,
                    "source": source,
                    "bootstrap_time": result["bootstrap_time"],
                }),
                now,
            ),
        )
        await store._db.commit()
    except Exception as e:
        # 补充审计日志失败不影响 bootstrap 成功(主审计日志已由 cache_store 写入)
        logger.debug(
            f"[bootstrap_admin_principal_atomic] 补充审计日志写入失败(可忽略): {e}"
        )

    result["success"] = True
    logger.info(
        f"[bootstrap_admin_principal_atomic] 原子 bootstrap 成功 "
        f"principal={principal_id} user={username} roles={roles} source={source}"
    )
    return result


async def main():
    """CLI 入口: python -m services.bootstrap_runner

    用法:
      python -m services.bootstrap_runner              # 幂等加载(已有数据跳过)
      python -m services.bootstrap_runner --force      # 强制重新加载
      python -m services.bootstrap_runner --tables cells,users  # 只加载指定表
      python -m services.bootstrap_runner --bootstrap-admin     # 仅执行 admin principal bootstrap
    """
    import sys
    force = "--force" in sys.argv
    tables = None
    if "--tables" in sys.argv:
        idx = sys.argv.index("--tables")
        if idx + 1 < len(sys.argv):
            tables = [t.strip() for t in sys.argv[idx + 1].split(",") if t.strip()]

    # R45 §7.1: --bootstrap-admin 仅执行 admin principal bootstrap
    if "--bootstrap-admin" in sys.argv:
        try:
            result = await bootstrap_admin_principal_atomic(source="cli")
            if result["success"]:
                logger.info(
                    f"[bootstrap_runner] admin principal bootstrap 成功 "
                    f"(skipped={result['skipped']}, principal_id={result['principal_id']})"
                )
            else:
                logger.error(
                    f"[bootstrap_runner] admin principal bootstrap 失败: {result['error']}"
                )
                sys.exit(2)
        except Exception as e:
            logger.exception(f"[bootstrap_runner] admin principal bootstrap 异常: {e}")
            sys.exit(2)
        return

    try:
        result = await run_bootstrap(force=force, tables=tables)
        if result["errors"]:
            logger.warning(
                f"[bootstrap_runner] 完成但有 {len(result['errors'])} 错误: "
                f"{result['errors'][:3]}"
            )
        logger.info("[bootstrap_runner] bootstrap 成功完成,退出码 0")
    except Exception as e:
        logger.exception(f"[bootstrap_runner] bootstrap 失败: {e}")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
