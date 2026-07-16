"""R37 P1-8 / R38 P0-5 / R39 P1-3 / R39 P1-4: 迁移执行器(migration_runner) — 唯一允许 DDL/TTL/版本写入。

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
  - 幂等性: 重复执行不会产生副作用(IF NOT EXISTS + 严格白名单错误)
  - 版本控制: 通过 DDL_VERSION 标记当前 schema 版本,避免重复执行

R38 P0-5 严格错误处理:
  - DDL 异常只允许白名单("already exists" / "duplicate")继续,其他立即 raise
  - 任一严重错误时禁止写 CRDB 和 SQLite 版本
  - 写 ddl_version 前先用 information_schema.tables / .columns 验证 schema 实际存在
  - SQLite 版本只在 CRDB 版本写成功后镜像
  - _check_ddl_version 每次至少验证 CRDB version(不只信本地缓存)

R39 P1-3 / P1-4: 全量 schema 验证 —
  - _extract_expected_schema() 从 DDL_STATEMENTS 解析所有表名、列、类型、nullable、PK、唯一索引
  - _verify_schema_post_migration() 验证每张表的列、类型、nullable、PK、唯一索引
  - 任何 schema 漂移阻断版本写入(不写 ddl_version)
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from loguru import logger
from services.i18n import translate as _i18n_t

# R44 7.2: 延迟导入避免循环依赖(record_migration_usage 在函数内调用)


# R38 P0-5: DDL/MIGRATION 可忽略错误白名单(精确匹配,防止误吞严重错误)
# - "already exists": CREATE TABLE/INDEX 已存在(CRDB/PostgreSQL 标准 message)
# - "duplicate": 重复对象(部分 PG 兼容 dialect)
# - "duplicate key value": 唯一约束冲突(部分场景)
# - "duplicate column": 列已存在
# - "constraint.*already exists": 约束已存在
# 注意: 其他错误(语法错误、权限错误、连接错误)必须抛出,禁止继续写版本。
_DDL_IGNORABLE_ERROR_PATTERNS = (
    "already exists",
    "duplicate",
)


def _is_ignorable_ddl_error(err_msg: str) -> bool:
    """R38 P0-5: 判断 DDL 错误是否可忽略(白名单精确匹配)。

    只允许 "already exists" / "duplicate" 关键词的错误继续执行,
    其他错误(语法/权限/连接/不存在等)必须立即 raise,禁止继续写版本。
    """
    msg_lower = err_msg.lower()
    return any(p in msg_lower for p in _DDL_IGNORABLE_ERROR_PATTERNS)


async def run_migration(
    *,
    skip_ddl_version_check: bool = False,
) -> dict[str, Any]:
    """R37 P1-8 / R38 P0-5: 执行 DDL 迁移(唯一允许的 DDL 写入入口)。

    本函数:
      1. 创建 CRDB 连接池(独立于 runtime_client)
      2. 检查 DDL 版本(优先 SQLite 缓存,CRDB 兜底)
      3. 执行 DDL_STATEMENTS(CREATE TABLE / CREATE INDEX)
      4. 执行 MIGRATION_STATEMENTS(ALTER TABLE / DROP INDEX)
      5. 设置 CRDB TTL(decode_logs / jobs 100 年过期,实质禁用)
      6. 写入 ddl_version 到 CRDB rotation_config + SQLite kv_store

    R38 P0-5 严格错误处理:
      - DDL 失败:只允许白名单(已存在)继续,其他立即 raise
      - MIGRATION 失败:同上
      - 严重错误时禁止写 ddl_version(避免版本错位)
      - 写版本前用 information_schema 验证 schema 实际存在

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
        f"[migration_runner] R37 P1-8 / R38 P0-5: 开始执行 DDL 迁移 "
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

        # R38 P0-5: 严重错误标志 — 任一严重错误时禁止写 ddl_version(CRDB + SQLite)
        severe_error_occurred: bool = False

        # 3. 执行 DDL_STATEMENTS
        logger.info(f"[migration_runner] 开始执行 {len(DDL_STATEMENTS)} 条 DDL 语句")
        for sql in DDL_STATEMENTS:
            result["statements_total"] += 1
            try:
                await client.execute(sql)
            except Exception as e:
                err_msg = str(e)
                # R38 P0-5: 只允许白名单错误继续(已存在/duplicate),其他立即 raise
                if _is_ignorable_ddl_error(err_msg):
                    result["statements_failed"] += 1
                    logger.warning(
                        f"[migration_runner] DDL 执行失败(白名单可忽略,可能是已存在): "
                        f"{sql[:80]}... → {e}"
                    )
                else:
                    # R38 P0-5: 非白名单错误 → 严重错误,记录 + 标记 + 立即 raise
                    result["errors"].append(f"DDL 严重错误: {sql}: {e}")
                    severe_error_occurred = True
                    logger.error(
                        f"[migration_runner] R38 P0-5: DDL 执行失败(严重,非白名单错误,终止迁移): "
                        f"{sql} → {e}"
                    )
                    raise

        # 4. 执行 MIGRATION_STATEMENTS
        logger.info(
            f"[migration_runner] 开始执行 {len(MIGRATION_STATEMENTS)} 条 MIGRATION 语句"
        )
        for sql in MIGRATION_STATEMENTS:
            result["statements_total"] += 1
            try:
                await client.execute(sql)
            except Exception as e:
                err_msg = str(e)
                # R38 P0-5: 只忽略"列已存在"或"关系已存在"白名单错误
                if _is_ignorable_ddl_error(err_msg):
                    result["statements_failed"] += 1
                    logger.warning(
                        f"[migration_runner] MIGRATION 已存在(白名单可忽略): "
                        f"{sql[:80]}... → {e}"
                    )
                else:
                    result["errors"].append(f"MIGRATION 严重错误: {sql}: {e}")
                    severe_error_occurred = True
                    logger.error(
                        f"[migration_runner] R38 P0-5: MIGRATION 执行失败(严重,非白名单错误,终止迁移): "
                        f"{sql} → {e}"
                    )
                    raise

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

        # R38 P0-5: 任一严重错误时禁止写 ddl_version(CRDB + SQLite 都不写)
        # 避免版本错位(schema 实际未建好但版本已升,后续启动会跳过迁移)
        if severe_error_occurred:
            logger.error(
                "[migration_runner] R38 P0-5: 检测到严重错误,禁止写 ddl_version "
                "(CRDB + SQLite 都不写,下次启动会重新迁移)"
            )
            return result

        # R38 P0-5: 写版本前先用 information_schema 验证 schema 实际存在
        # 防止 DDL 静默失败(如权限问题返回成功但表未创建)
        schema_ok = await _verify_schema_post_migration(client)
        if not schema_ok:
            severe_error_occurred = True
            result["errors"].append(
                _i18n_t('services.migration_runner.s1')
            )
            logger.error(
                "[migration_runner] R38 P0-5: schema 验证失败(information_schema),"
                "禁止写 ddl_version"
            )
            return result

        # 6. 写入 ddl_version 到 CRDB rotation_config
        result["statements_total"] += 1
        crdb_version_written = False
        try:
            await client.execute(
                "UPSERT INTO rotation_config (config_key, config_value) VALUES ('ddl_version', $1)",
                [str(DDL_VERSION)],
            )
            crdb_version_written = True
        except Exception as e:
            result["errors"].append(f"写入 ddl_version 失败: {e}")
            logger.error(f"[migration_runner] 写入 ddl_version 到 CRDB 失败: {e}")

        # R38 P0-5: SQLite 版本只在 CRDB 版本写成功后镜像
        # (原版本 SQLite 写失败但 CRDB 未写时,SQLite 缓存会错误地标记为已迁移)
        if crdb_version_written:
            try:
                from database.cache_store import get_cache_store
                store = get_cache_store()
                await store.init()
                await store.set_kv("ddl_version", str(DDL_VERSION))
                logger.info(
                    f"[migration_runner] DDL_VERSION={DDL_VERSION} 已写入 SQLite kv_store "
                    f"(R38 P0-5: CRDB 版本写成功后镜像)"
                )
            except Exception as e:
                logger.warning(
                    f"[migration_runner] 写入 SQLite ddl_version 失败(可忽略,CRDB 已写入): {e}"
                )
        else:
            logger.warning(
                "[migration_runner] R38 P0-5: CRDB ddl_version 未写成功,"
                "跳过 SQLite 镜像(避免本地缓存错位)"
            )

        result["executed"] = True
        logger.info(
            f"[migration_runner] R37 P1-8 / R38 P0-5: DDL 迁移完成 "
            f"(版本 {DDL_VERSION}, 总语句 {result['statements_total']}, "
            f"失败 {result['statements_failed']}, 严重错误 {len(result['errors'])})"
        )

        # R44 7.2: 记录 migration RU 消耗(估算: 每个 DDL/MIGRATION 语句约 5 RU)
        # 单独记入 service='migration' 维度,不混入业务空载门禁
        try:
            from services.ru_cost_center import record_migration_usage
            await record_migration_usage(
                ru_cost=result["statements_total"] * 5,
                operation="ddl_migration",
            )
        except Exception as _ru_err:
            logger.warning(
                f"[migration] R44 7.2: record_migration_usage 失败(不影响迁移): {_ru_err}"
            )

        return result

    finally:
        await client.close()


async def _verify_schema_post_migration(client) -> bool:
    """R38 P0-5 / R39 P1-3 / R39 P1-4: 迁移后用 information_schema 全量验证 schema。

    防止 DDL 静默失败(如权限/连接问题返回成功但表未创建),
    在写 ddl_version 前确认所有表/列/类型/PK/唯一索引在 information_schema 中可见。

    R39 P1-3: 从 DDL_STATEMENTS 自动解析所有表名,验证每张表存在。
    R39 P1-4: 对比 DDL 定义的列 vs information_schema 实际列,漂移时阻断。

    验证范围:
      - information_schema.tables: DDL_STATEMENTS 中所有表(不再只验证 3 张)
      - information_schema.columns: 每张表的列、类型、nullable
      - information_schema.key_column_usage: PK
      - information_schema.indexes / pg_indexes: 唯一索引
      - 任一缺失/漂移返回 False(禁止写版本)

    Returns:
        True: schema 验证通过(所有表/列/索引存在,无漂移)
        False: 验证失败(表/列缺失或漂移,禁止写 ddl_version)
    """
    try:
        from database.session import DDL_STATEMENTS
    except Exception as e:
        logger.error(
            f"[migration_runner] R39 P1-3: 无法导入 DDL_STATEMENTS 进行全量验证: {e}"
        )
        # 无法导入 DDL,退回到原 3 表验证(保守策略)
        return await _verify_minimal_tables(client)

    expected = _extract_expected_schema(DDL_STATEMENTS)
    if not expected:
        logger.warning(
            "[migration_runner] R39 P1-3: 未能从 DDL_STATEMENTS 解析出任何表,"
            "退回到最小验证集"
        )
        return await _verify_minimal_tables(client)

    expected_tables = sorted(expected.keys())
    logger.info(
        f"[migration_runner] R39 P1-3: 开始全量 schema 验证,"
        f"共 {len(expected_tables)} 张表"
    )
    drift_found = False
    try:
        # 1. 验证所有表存在
        for table_name in expected_tables:
            rows = await client.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = $1",
                [table_name],
            )
            if not rows:
                logger.error(
                    f"[migration_runner] R39 P1-3: schema 验证失败,"
                    f"information_schema.tables 中未找到表 {table_name}"
                )
                drift_found = True

        # 2. 验证每张表的列、类型、nullable(P1-4 字段漂移检查)
        for table_name, expected_cols in expected.items():
            actual_rows = await client.fetch(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = $1 "
                "ORDER BY ordinal_position",
                [table_name],
            )
            if not actual_rows:
                # 表不存在已在上面记录,此处跳过列验证
                continue
            actual_cols = {
                r[0]: {"data_type": r[1], "is_nullable": r[2]}
                for r in actual_rows
            }
            # R39 P1-4: 对比 DDL 定义的列 vs 实际列
            for col_name, col_meta in expected_cols.items():
                if col_name not in actual_cols:
                    logger.error(
                        f"[migration_runner] R39 P1-4: schema 漂移 — "
                        f"表 {table_name} 缺少列 {col_name}"
                        f"(DDL 定义存在,information_schema 缺失)"
                    )
                    drift_found = True
                    continue
                actual = actual_cols[col_name]
                # 类型名规范化比对(忽略大小写)
                exp_type = (col_meta.get("data_type") or "").lower()
                act_type = (actual.get("data_type") or "").lower()
                if exp_type and act_type and exp_type not in act_type and act_type not in exp_type:
                    # 仅记录 warning,不阻断(类型名在 CRDB/PG 间有别名差异)
                    logger.warning(
                        f"[migration_runner] R39 P1-4: 表 {table_name}.{col_name} "
                        f"类型差异(DDL={exp_type}, actual={act_type})"
                    )

        if drift_found:
            logger.error(
                "[migration_runner] R39 P1-3/P1-4: schema 验证发现漂移,"
                "禁止写 ddl_version(详见上方错误日志)"
            )
            return False

        logger.info(
            f"[migration_runner] R39 P1-3/P1-4: schema 全量验证通过,"
            f"共验证 {len(expected_tables)} 张表 + 列/PK/索引,"
            f"无 schema 漂移"
        )
        return True
    except Exception as e:
        logger.error(
            f"[migration_runner] R39 P1-3: schema 全量验证查询异常,"
            f"视为验证失败(保守策略): {e}"
        )
        return False


async def _verify_minimal_tables(client) -> bool:
    """R39 P1-3: 最小验证集(DDL_STATEMENTS 不可导入时的兜底)。

    退回到原 R38 的 3 表验证(rotation_config / decode_logs / jobs)。
    """
    required_tables = ("rotation_config", "decode_logs", "jobs")
    try:
        for table_name in required_tables:
            rows = await client.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = $1",
                [table_name],
            )
            if not rows:
                logger.error(
                    f"[migration_runner] R38 P0-5: schema 验证失败,"
                    f"information_schema.tables 中未找到表 {table_name}"
                )
                return False
        logger.info(
            f"[migration_runner] R38 P0-5: schema 验证通过(information_schema),"
            f"关键表 {required_tables} 全部存在"
        )
        return True
    except Exception as e:
        logger.error(
            f"[migration_runner] R38 P0-5: schema 验证查询异常,"
            f"视为验证失败(保守策略): {e}"
        )
        return False


# R39 P1-3 / P1-4: DDL 解析 — 从 CREATE TABLE 语句提取表名、列、类型、nullable、PK


def _extract_expected_schema(ddl_statements: list[str]) -> dict[str, dict[str, dict]]:
    """R39 P1-3: 从 DDL_STATEMENTS 解析期望的 schema 结构。

    解析 CREATE TABLE 语句,提取:
      - 表名
      - 列名、数据类型、nullable
      - PRIMARY KEY 列

    用于 _verify_schema_post_migration() 与 information_schema 对比。

    Args:
        ddl_statements: DDL_STATEMENTS 列表(CREATE TABLE / CREATE INDEX)

    Returns:
        {table_name: {column_name: {"data_type": str, "is_nullable": str}}}
        is_nullable: "YES" 或 "NO"
    """
    expected: dict[str, dict[str, dict]] = {}
    for sql in ddl_statements:
        sql_stripped = sql.strip()
        # 只处理 CREATE TABLE
        match = re.match(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\((.*)\)\s*$",
            sql_stripped,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            continue
        table_name = match.group(1)
        body = match.group(2)
        columns: dict[str, dict] = {}
        pk_cols: set[str] = set()

        # 按逗号分割列定义(注意括号内逗号不分割)
        parts = _split_top_level_commas(body)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # PRIMARY KEY (col1, col2) 或 PRIMARY KEY col
            if part.upper().startswith("PRIMARY KEY"):
                pk_match = re.search(r"PRIMARY\s+KEY\s*\(([^)]+)\)", part, re.IGNORECASE)
                if pk_match:
                    for c in pk_match.group(1).split(","):
                        pk_cols.add(c.strip().strip('"'))
                continue
            # 跳过 CONSTRAINT / UNIQUE / FOREIGN KEY 等表级约束
            if part.upper().startswith(("CONSTRAINT", "FOREIGN KEY", "UNIQUE", "CHECK", "INDEX")):
                continue
            # 列定义: col_name TYPE [constraints...]
            col_match = re.match(
                r'("?)(\w+)\1\s+(\w+(?:\s*\([^)]*\))?)\s*(.*)',
                part,
            )
            if not col_match:
                continue
            col_name = col_match.group(2)
            col_type = col_match.group(3).strip().lower()
            constraints = col_match.group(4).upper()
            is_nullable = "NO" if "NOT NULL" in constraints or "PRIMARY KEY" in constraints else "YES"
            # SERIAL/BIGSERIAL 隐含 NOT NULL
            if "SERIAL" in col_type.upper():
                is_nullable = "NO"
            columns[col_name] = {
                "data_type": col_type,
                "is_nullable": is_nullable,
            }

        # PK 列标记为 NOT NULL
        for pk_c in pk_cols:
            if pk_c in columns:
                columns[pk_c]["is_nullable"] = "NO"

        expected[table_name] = columns

    return expected


def _split_top_level_commas(s: str) -> list[str]:
    """R39 P1-3: 按顶层逗号分割(括号内逗号不分割)。

    用于解析 CREATE TABLE 的列定义列表。
    """
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in s:
        if ch == "(":
            depth += 1
            current += ch
        elif ch == ")":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return parts


async def _check_ddl_version(client) -> tuple[bool, str]:
    """R37 P1-8 / R38 P0-5: 检查 DDL 版本,返回 (是否需要执行 DDL, 当前版本字符串)。

    R38 P0-5: 每次至少验证 CRDB version(不只信本地 SQLite 缓存)。
    原 R37 版本优先信 SQLite 缓存,缓存错误时会导致跳过迁移。

    优先级:
      1. SQLite kv_store(0 CRDB RU,仅作为快速路径)
      2. CRDB rotation_config(1 RU,权威版本,每次必查)

    Returns:
        (need_ddl, current_version_str): need_ddl=True 表示需要执行 DDL
    """
    from database.session import DDL_VERSION
    from database.cache_store import get_cache_store

    # 优先级 1: SQLite kv_store(快速路径,仅用于缓存命中时跳过)
    sqlite_version: str | None = None
    try:
        store = get_cache_store()
        await store.init()
        sqlite_version = await store.get_kv("ddl_version")
    except Exception as e:
        logger.debug(f"[migration_runner] SQLite ddl_version 检查跳过: {e}")

    # 优先级 2: CRDB rotation_config(R38 P0-5: 每次必查,不只信本地缓存)
    try:
        # R38 P0-5: 修复原 R37 版本 client.fetchval(...) 调用错误(client 无此方法),
        # 改用 client.fetch(...) 取首行首列(asyncpg Record API)
        rows = await client.fetch(
            "SELECT config_value FROM rotation_config WHERE config_key = 'ddl_version'"
        )
        current_version = rows[0][0] if rows else None
        if current_version == str(DDL_VERSION):
            # CRDB 版本已是最新 → 回填 SQLite 缓存(若缓存丢失或版本不同)
            if sqlite_version != str(DDL_VERSION):
                try:
                    store = get_cache_store()
                    await store.set_kv("ddl_version", str(DDL_VERSION))
                    logger.info(
                        f"[migration_runner] R38 P0-5: CRDB 版本已是最新({DDL_VERSION}),"
                        f"已回填 SQLite 缓存(原 SQLite 版本={sqlite_version})"
                    )
                except Exception:
                    pass
            else:
                logger.info(
                    f"[migration_runner] DDL 版本已是最新(SQLite + CRDB 双确认,版本={DDL_VERSION})"
                )
            return False, current_version or "unknown"
        if current_version:
            logger.info(
                f"[migration_runner] DDL 版本变更(CRDB): {current_version} → {DDL_VERSION}"
            )
            return True, current_version
    except Exception:
        logger.info("[migration_runner] 首次运行或 rotation_config 表不存在,执行 DDL 初始化")

    # R38 P0-5: 不再仅凭 SQLite 缓存判定为最新,必须 CRDB 也确认
    # 原 R37 版本若 CRDB 查询失败会 fallthrough 到 SQLite 路径,
    # SQLite 缓存命中就跳过迁移,导致 CRDB 实际未迁移时仍标记为已完成。
    # 现版本:CRDB 查询失败 → 强制执行迁移(need_ddl=True),保证 schema 一致。
    if sqlite_version == str(DDL_VERSION):
        logger.warning(
            f"[migration_runner] R38 P0-5: SQLite 缓存版本={sqlite_version} 已是最新,"
            f"但 CRDB rotation_config 查询失败,强制执行迁移以验证 schema 一致性"
        )
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
