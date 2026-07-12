"""R37 P1-8: 迁移执行器(migration_runner) — 唯一允许 DDL/TTL/版本写入。

职责边界(P1-8 拆分):
  - migration_runner: 唯一允许 DDL_STATEMENTS / MIGRATION_STATEMENTS / TTL / 版本写入
  - bootstrap_runner: 显式人工或恢复任务,限速且可观测
  - runtime_client(database.session.init_db): 只连接/查询,不导入 DDL,不 bootstrap

调用方:
  - systemd migration oneshot 服务: python -m services.migration_runner
  - docker-compose migration 服务: python -m services.migration_runner
  - 严禁业务 Bot / crdb_sync / db_writer 调用此模块(它们应只连 runtime)

设计原则:
  - 唯一性: 全系统只有此模块可执行 DDL/MIGRATION/TTL 写入
  - 可观测: 每条 SQL 执行结果记录到日志,便于审计
  - 幂等性: 重复执行不会产生副作用(IF NOT EXISTS + try/except)
  - 版本控制: 通过 DDL_VERSION 标记当前 schema 版本,避免重复执行
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from loguru import logger


async def run_migration(
    *,
    skip_ddl_version_check: bool = False,
) -> dict[str, Any]:
    """R37 P1-8: 执行 DDL 迁移(唯一允许的 DDL 写入入口)。

    本函数:
      1. 创建 CRDB 连接池(独立于 runtime_client)
      2. 检查 DDL 版本(优先 SQLite 缓存,CRDB 兜底)
      3. 执行 DDL_STATEMENTS(CREATE TABLE / CREATE INDEX)
      4. 执行 MIGRATION_STATEMENTS(ALTER TABLE / DROP INDEX)
      5. 设置 CRDB TTL(decode_logs / jobs 100 年过期,实质禁用)
      6. 写入 ddl_version 到 CRDB rotation_config + SQLite kv_store

    Args:
        skip_ddl_version_check: True 跳过版本检查强制执行(用于初始化或修复)

    Returns:
        {
            "executed": bool,           # 是否实际执行了 DDL
            "ddl_version": int,         # 当前 DDL 版本
            "before_version": str,      # 执行前版本("unknown" 如首次)
            "statements_total": int,    # 总执行 SQL 数
            "statements_failed": int,    # 失败 SQL 数(可忽略错误)
            "errors": [str],            # 失败错误列表(仅严重错误)
        }
    """
    # 延迟导入避免循环依赖
    from database.session import DDL_STATEMENTS, MIGRATION_STATEMENTS, DDL_VERSION
    from database.session import CockroachDBClient
    from config import settings as _settings

    logger.info(
        f"[migration_runner] R37 P1-8: 开始执行 DDL 迁移 "
        f"(DDL_VERSION={DDL_VERSION}, role={_settings.SERVICE_ROLE})"
    )

    result: dict[str, Any] = {
        "executed": False,
        "ddl_version": DDL_VERSION,
        "before_version": "unknown",
        "statements_total": 0,
        "statements_failed": 0,
        "errors": [],
    }

    # 1. 创建独立连接池(migration 专用,不污染 runtime pool)
    client = CockroachDBClient()
    client.configure(_settings.COCKROACHDB_URL)
    await client.connect_runtime_only()

    try:
        # 2. 检查 DDL 版本(优先 SQLite,CRDB 兜底)
        if not skip_ddl_version_check:
            need_ddl, before_version = await _check_ddl_version(client)
            result["before_version"] = before_version
            if not need_ddl:
                logger.info(
                    f"[migration_runner] DDL 版本已是最新({DDL_VERSION}),跳过迁移"
                )
                return result

        # 3. 执行 DDL_STATEMENTS
        logger.info(f"[migration_runner] 开始执行 {len(DDL_STATEMENTS)} 条 DDL 语句")
        for sql in DDL_STATEMENTS:
            result["statements_total"] += 1
            try:
                await client.execute(sql)
            except Exception as e:
                result["statements_failed"] += 1
                logger.warning(
                    f"[migration_runner] DDL 执行失败(可忽略,可能是已存在): "
                    f"{sql[:80]}... → {e}"
                )

        # 4. 执行 MIGRATION_STATEMENTS
        logger.info(
            f"[migration_runner] 开始执行 {len(MIGRATION_STATEMENTS)} 条 MIGRATION 语句"
        )
        for sql in MIGRATION_STATEMENTS:
            result["statements_total"] += 1
            try:
                await client.execute(sql)
            except Exception as e:
                err_msg = str(e).lower()
                # 只忽略"列已存在"或"关系已存在"错误
                if "already exists" in err_msg or "duplicate" in err_msg:
                    result["statements_failed"] += 1
                    logger.warning(
                        f"[migration_runner] MIGRATION 已存在(可忽略): "
                        f"{sql[:80]}... → {e}"
                    )
                else:
                    result["errors"].append(f"{sql}: {e}")
                    logger.error(
                        f"[migration_runner] MIGRATION 执行失败(严重): {sql} → {e}"
                    )

        # 5. 设置 CRDB TTL(等待 schema change 完成)
        # ADD COLUMN 是异步 schema change,紧跟 TTL 修改会报错
        logger.info("[migration_runner] 等待 schema change 完成(3s)...")
        await asyncio.sleep(3)
        ttl_statements = [
            "ALTER TABLE decode_logs SET (ttl_expiration_expression = 'CAST(request_time AS TIMESTAMPTZ) + INTERVAL ''100 years''', ttl_job_cron = '@yearly')",
            "ALTER TABLE jobs SET (ttl_expiration_expression = 'CAST(created_at AS TIMESTAMPTZ) + INTERVAL ''100 years''', ttl_job_cron = '@yearly')",
        ]
        for ttl_sql in ttl_statements:
            result["statements_total"] += 1
            for attempt in range(3):
                try:
                    await client.execute(ttl_sql)
                    break
                except Exception as e:
                    if "another schema change" in str(e).lower() and attempt < 2:
                        logger.warning(
                            f"[migration_runner] TTL 等待 schema change,重试 "
                            f"{attempt + 1}/3: {e}"
                        )
                        await asyncio.sleep(5)
                    else:
                        result["statements_failed"] += 1
                        logger.warning(f"[migration_runner] TTL 设置失败(可忽略): {e}")
                        break

        # 6. 写入 ddl_version 到 CRDB rotation_config
        result["statements_total"] += 1
        try:
            await client.execute(
                "UPSERT INTO rotation_config (config_key, config_value) VALUES ('ddl_version', $1)",
                [str(DDL_VERSION)],
            )
        except Exception as e:
            result["errors"].append(f"写入 ddl_version 失败: {e}")
            logger.error(f"[migration_runner] 写入 ddl_version 到 CRDB 失败: {e}")

        # 7. 写入 SQLite kv_store(后续 runtime 启动 0 CRDB RU)
        try:
            from database.cache_store import get_cache_store
            store = get_cache_store()
            await store.init()
            await store.set_kv("ddl_version", str(DDL_VERSION))
            logger.info(
                f"[migration_runner] DDL_VERSION={DDL_VERSION} 已写入 SQLite kv_store"
            )
        except Exception as e:
            logger.warning(
                f"[migration_runner] 写入 SQLite ddl_version 失败(可忽略,CRDB 已写入): {e}"
            )

        result["executed"] = True
        logger.info(
            f"[migration_runner] R37 P1-8: DDL 迁移完成 "
            f"(版本 {DDL_VERSION}, 总语句 {result['statements_total']}, "
            f"失败 {result['statements_failed']}, 严重错误 {len(result['errors'])})"
        )
        return result

    finally:
        await client.close()


async def _check_ddl_version(client) -> tuple[bool, str]:
    """检查 DDL 版本,返回 (是否需要执行 DDL, 当前版本字符串)。

    优先级:
      1. SQLite kv_store(0 CRDB RU)
      2. CRDB rotation_config(1 RU,首次启动或 SQLite 缓存丢失时)

    Returns:
        (need_ddl, current_version_str): need_ddl=True 表示需要执行 DDL
    """
    from database.session import DDL_VERSION
    from database.cache_store import get_cache_store

    # 优先级 1: SQLite kv_store
    try:
        store = get_cache_store()
        await store.init()
        ddl_version = await store.get_kv("ddl_version")
        if ddl_version == str(DDL_VERSION):
            return False, ddl_version or "unknown"
        if ddl_version:
            logger.info(
                f"[migration_runner] DDL 版本变更(SQLite): {ddl_version} → {DDL_VERSION}"
            )
            return True, ddl_version
    except Exception as e:
        logger.debug(f"[migration_runner] SQLite ddl_version 检查跳过: {e}")

    # 优先级 2: CRDB rotation_config
    try:
        current_version = await client.fetchval(
            "SELECT config_value FROM rotation_config WHERE config_key = 'ddl_version'"
        )
        if current_version == str(DDL_VERSION):
            # 回填 SQLite 缓存
            try:
                store = get_cache_store()
                await store.set_kv("ddl_version", str(DDL_VERSION))
            except Exception:
                pass
            return False, current_version or "unknown"
        if current_version:
            logger.info(
                f"[migration_runner] DDL 版本变更(CRDB): {current_version} → {DDL_VERSION}"
            )
            return True, current_version
    except Exception:
        logger.info("[migration_runner] 首次运行或 rotation_config 表不存在,执行 DDL 初始化")

    return True, "unknown"


async def main():
    """CLI 入口: python -m services.migration_runner"""
    from database.session import close_db
    try:
        result = await run_migration()
        if result["errors"]:
            logger.error(
                f"[migration_runner] 迁移完成但有 {len(result['errors'])} 严重错误: "
                f"{result['errors'][:3]}"
            )
            # 退出码 1 表示有严重错误(但 DDL 本身已执行)
            import sys
            sys.exit(1)
        logger.info("[migration_runner] 迁移成功完成,退出码 0")
    except Exception as e:
        logger.exception(f"[migration_runner] 迁移失败: {e}")
        import sys
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
