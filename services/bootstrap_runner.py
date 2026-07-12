"""R37 P1-8: 引导执行器(bootstrap_runner) — 显式人工或恢复任务,限速且可观测。

职责边界(P1-8 拆分):
  - migration_runner: 唯一允许 DDL/TTL/版本写入
  - bootstrap_runner: 显式人工或恢复任务,限速且可观测(此模块)
  - runtime_client(database.session.init_db): 只连接/查询,不导入 DDL,不 bootstrap

职责:
  - 预填充 cells 快照到 SQLite(避免 Mon Bot 首次运行回退到 CRDB)
  - 全表缓存热路径到 SQLite(users / codes / file_records / external_code_mapping)
  - 启动时从 CRDB 全量加载,之后所有读操作走 SQLite(0 CRDB RU)

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


async def main():
    """CLI 入口: python -m services.bootstrap_runner

    用法:
      python -m services.bootstrap_runner              # 幂等加载(已有数据跳过)
      python -m services.bootstrap_runner --force      # 强制重新加载
      python -m services.bootstrap_runner --tables cells,users  # 只加载指定表
    """
    import sys
    force = "--force" in sys.argv
    tables = None
    if "--tables" in sys.argv:
        idx = sys.argv.index("--tables")
        if idx + 1 < len(sys.argv):
            tables = [t.strip() for t in sys.argv[idx + 1].split(",") if t.strip()]

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
