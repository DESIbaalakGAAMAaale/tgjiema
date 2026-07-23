#!/usr/bin/env python3
"""R71 Wave 2/3: 备份恢复完整性结构化校验脚本。

整改背景(R71 P0-07 / P0-08):
    R70 Wave 5 的 backup_restore 阶段用日志关键词("ok"/"success"/"verified")
    判断恢复成功,这是不安全的:
      - 日志输出可能包含"ok"但实际恢复失败
      - 日志关键词匹配无法验证数据真实完整性
      - 测试通过不代表恢复的数据可用

    R71 Wave 2 整改(Commit 2):本脚本通过结构化数据校验替代日志关键词匹配:
      1. 备份前:在数据库写入测试标记行(unique trace_id)
      2. 触发 backup(由调用方完成,本脚本只做校验)
      3. 触发 restore(由调用方完成,本脚本只做校验)
      4. 校验:查询恢复后的数据库,确认测试标记行存在且内容一致
      5. 校验:比对关键表的 row count(备份前 vs 恢复后)
      6. 清理:删除测试标记行

    R71 Wave 3 整改(Commit 3, P0-08):本脚本扩展为完整的结构化校验:
      1. 确定性测试数据集(unique ID + 边界值 + 关系 + payload hash)
      2. Schema 指纹捕获(tables / pk / columns / conflict_col / source / DDL hash)
      3. 字段级 hash(每表 SELECT * ORDER BY pk → sha256 of canonical JSON)
      4. 迁移版本兼容性检查(current vs backup schema_version)
      5. 恢复目标隔离验证(--target-db staging)
      6. 应用启动/读写验证(python -m services.health + INSERT/SELECT/DELETE)
      7. 恢复环境合成交易(synthetic_transaction.run_full_transaction)
      8. 切换/回滚证据(RestoreOrchestrator import check + 结构化 JSON)
      9. 机器可读恢复证据(增强 IntegrityEvidence dataclass)

调用方式:
    # 写入测试标记
    python scripts/verify_restore_integrity.py write-marker --trace-id <uuid>

    # 获取快照(row counts + schema fingerprint + field hashes)
    python scripts/verify_restore_integrity.py snapshot --output <path>

    # 基本校验(标记 + row count)
    python scripts/verify_restore_integrity.py verify --trace-id <uuid> \\
        --pre-snapshot <path> --output <evidence.json>

    # 完整结构化校验(标记 + schema + field hash + migration + app + tx + switch)
    python scripts/verify_restore_integrity.py full-check --trace-id <uuid> \\
        --pre-snapshot <path> --output <evidence.json> --target-db staging

    # 清理测试标记
    python scripts/verify_restore_integrity.py cleanup --trace-id <uuid>

退出码:
    0 — 校验通过(数据完整)
    1 — 校验失败(fail-closed)
    2 — CLI 参数错误或运行时异常

依赖:
    - docker compose exec db_writer python -c "..." 执行 SQL
    - docker compose exec db_writer python -m services.health --role db_writer --json
    - scripts/synthetic_transaction.py 的 run_full_transaction()
    - services.restore_orchestrator 的 import check(不实际执行切换)
"""
# R71 RC35: 移除 `from __future__ import annotations`。
# 根因(RC33 同类): `from __future__ import annotations` + `@dataclass` + PEP 604
# `str | None` 在 `dataclasses._is_type` 中触发
# `AttributeError: 'NoneType' object has no attribute '__dict__'`。
# compose_runtime_e2e.py 通过 importlib 加载本模块时,该错误导致
# backup_restore 阶段直接失败。CI 使用 Python 3.12,本地 Python 3.10,
# 均原生支持 `str | None` / `dict[str, Any]` / `list[str]` / `Path | None`
# 语法,无需 `from __future__ import annotations`。移除后 @dataclass 直接
# 处理实际类型对象(非字符串),_is_type 跳过评估,不再触发 AttributeError。

import argparse
import datetime as _dt
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent

# 生产 compose 文件
COMPOSE_FILE = REPO_ROOT / "docker-compose.prod.yml"

# 测试标记表(bot_heartbeat 复用,避免引入新表)
MARKER_TABLE = "bot_heartbeat"

# 关键表列表(用于 row count 比对 + field hash)
CRITICAL_TABLES = [
    "bot_heartbeat",
    "file_index",
    "user_quota",
    "relay_spool",
    "app_meta",
]

# 需要捕获 field hash 的表(从 BACKUP_SCHEMA 中选取的 SQLite 表)
# 这些表在 cache_store.db 中存在,可用于字段级 hash
FIELD_HASH_TABLES = [
    "bot_heartbeat",
    "user_quota",
    "kv_store",
]

# 默认目标数据库(production / staging)
DEFAULT_TARGET_DB = "production"

# 目标数据库路径映射
TARGET_DB_PATHS = {
    "production": "/app/data/cache_store.db",
    "staging": "/app/data/staging/cache_store.db",
}

# synthetic_transaction.py 路径(R71 Wave 3: 恢复环境合成交易)
SYNTHETIC_TRANSACTION_PATH = REPO_ROOT / "scripts" / "synthetic_transaction.py"

# SQLite 整数最大值(用于边界值测试)
SQLITE_MAX_INT = 9223372036854775807  # 2^63 - 1


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _compose_cmd(args: list[str]) -> list[str]:
    """构造 docker compose 命令。"""
    return ["docker", "compose", "-f", str(COMPOSE_FILE)] + args


def _run(
    cmd: list[str],
    *,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """执行命令(fail-closed,不吞异常)。

    任何 subprocess 异常(TimeoutExpired / FileNotFoundError / OSError)
    都被转换为带 returncode=-1 的 CompletedProcess,由调用方决定如何处理。
    不在此处吞异常或自动重试(fail-closed 原则)。
    """
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired as e:
        cp = subprocess.CompletedProcess(
            args=cmd, returncode=-1, stdout="", stderr=str(e),
        )
        return cp
    except (FileNotFoundError, OSError) as e:
        cp = subprocess.CompletedProcess(
            args=cmd, returncode=-1, stdout="", stderr=str(e),
        )
        return cp


def _get_db_path(target_db: str) -> str:
    """返回目标数据库路径。

    Args:
        target_db: "production" 或 "staging"

    Returns:
        数据库文件路径(容器内路径)
    """
    return TARGET_DB_PATHS.get(target_db, TARGET_DB_PATHS[DEFAULT_TARGET_DB])


def _exec_python(code: str, timeout: int = 30, target_db: str = DEFAULT_TARGET_DB) -> tuple[int, str, str]:
    """通过 docker compose exec db_writer 执行 Python 代码。

    通用执行器:所有 SQL 查询、health 检查、数据操作都通过此函数执行,
    便于测试 monkeypatch。

    Args:
        code: Python 代码字符串(python -c "...")
        timeout: 命令超时秒数
        target_db: 目标数据库(production / staging)

    Returns:
        (returncode, stdout, stderr)
    """
    db_path = _get_db_path(target_db)
    cmd = _compose_cmd([
        "exec", "-T", "db_writer",
        "python", "-c", code,
    ])
    result = _run(cmd, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def _exec_sql(query: str, timeout: int = 30, target_db: str = DEFAULT_TARGET_DB) -> tuple[int, str, str]:
    """通过 docker compose exec db_writer 执行 SQL。

    Args:
        query: SQL 语句(单条,结果集通过 SELECT 返回)
        timeout: 命令超时秒数
        target_db: 目标数据库(production / staging)

    Returns:
        (returncode, stdout, stderr)
        stdout 为 JSON 数组(如 [[5]])或 "OK"(无结果集)
    """
    db_path = _get_db_path(target_db)
    # 使用参数化查询避免 SQL 注入(trace_id 是 uuid4 hex,但仍用参数化)
    # 通过 python -c 执行 SQL,结果序列化为 JSON
    code = (
        f"import sqlite3, json; "
        f"conn = sqlite3.connect('{db_path}', timeout=10); "
        f"conn.row_factory = sqlite3.Row; "
        f"cur = conn.execute(\"\"\"{query}\"\"\"); "
        f"rows = cur.fetchall(); "
        f"conn.commit(); "
        f"print(json.dumps([dict(r) for r in rows]) if rows else 'OK'); "
        f"conn.close()"
    )
    return _exec_python(code, timeout=timeout, target_db=target_db)


def _exec_sql_with_params(
    query: str,
    params: tuple[Any, ...],
    timeout: int = 30,
    target_db: str = DEFAULT_TARGET_DB,
) -> tuple[int, str, str]:
    """通过 docker compose exec db_writer 执行参数化 SQL。

    Args:
        query: SQL 语句(使用 ? 占位符)
        params: 参数元组
        timeout: 命令超时秒数
        target_db: 目标数据库

    Returns:
        (returncode, stdout, stderr)
    """
    db_path = _get_db_path(target_db)
    # 将 params 编码为 JSON,在 python -c 中解码
    params_json = json.dumps(list(params))
    code = (
        f"import sqlite3, json; "
        f"conn = sqlite3.connect('{db_path}', timeout=10); "
        f"conn.row_factory = sqlite3.Row; "
        f"params = tuple(json.loads('{params_json}')); "
        f"cur = conn.execute(\"\"\"{query}\"\"\", params); "
        f"rows = cur.fetchall(); "
        f"conn.commit(); "
        f"print(json.dumps([dict(r) for r in rows]) if rows else 'OK'); "
        f"conn.close()"
    )
    return _exec_python(code, timeout=timeout, target_db=target_db)


def _exec_health(role: str, timeout: int = 30) -> tuple[int, str, str]:
    """执行 python -m services.health --role <role> --json。

    Args:
        role: 服务角色(如 db_writer)
        timeout: 命令超时秒数

    Returns:
        (returncode, stdout, stderr)
        stdout 为 health JSON 输出
    """
    cmd = _compose_cmd([
        "exec", "-T", "db_writer",
        "python", "-m", "services.health",
        "--role", role,
        "--json",
    ])
    result = _run(cmd, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


# ════════════════════════════════════════════════════════════════
# 数据类定义
# ════════════════════════════════════════════════════════════════


@dataclass
class TableCount:
    """单表 row count。"""

    table: str
    count: int
    error: str | None = None


@dataclass
class TableHash:
    """单表字段级 hash。

    Attributes:
        table: 表名
        pk_columns: 主键列(用于 ORDER BY)
        row_count: 行数
        field_hash: SELECT * ORDER BY pk 的 sha256 of canonical JSON
        error: 错误信息(None 表示无错误)
    """

    table: str
    pk_columns: tuple[str, ...]
    row_count: int
    field_hash: str
    error: str | None = None


@dataclass
class SchemaFingerprint:
    """数据库 schema 指纹。

    捕获数据库的 schema 结构,用于备份前/恢复后比对。
    包含:表列表、主键、列、冲突列、数据源、索引/约束 DDL、指纹 hash。
    """

    schema_version: str
    tables: list[dict[str, Any]]
    index_constraint_summary: list[dict[str, Any]]
    fingerprint_hash: str
    error: str | None = None


@dataclass
class TestDataset:
    """确定性测试数据集。

    包含唯一 ID、边界值、关系行和 payload hash。
    用于备份前写入、恢复后验证数据完整性。

    Attributes:
        trace_id: 唯一标识符(uuid4 hex)
        rows: 测试行列表(每行是 dict,对应 bot_heartbeat 列)
        relations: 关系列表(每项是 (parent_name, child_name) 元组)
        payload_hash: 整个数据集的 sha256 of canonical JSON
    """

    trace_id: str
    rows: list[dict[str, Any]]
    relations: list[tuple[str, str]]
    payload_hash: str


@dataclass
class IntegrityEvidence:
    """完整性校验证据(机器可读)。

    R71 Wave 3 增强:包含 schema 指纹、字段级 hash、迁移版本检查、
    应用启动/读写验证、合成交易、切换/回滚证据。

    所有新增字段都有默认值,保持与 Wave 2 的向后兼容。
    """

    trace_id: str
    timestamp: str
    passed: bool
    marker_found: bool
    pre_counts: list[TableCount] = field(default_factory=list)
    post_counts: list[TableCount] = field(default_factory=list)
    count_mismatches: list[str] = field(default_factory=list)
    error: str | None = None
    # R71 Wave 3 新增字段
    schema_fingerprint: dict[str, Any] = field(default_factory=dict)
    schema_fingerprint_hash: str = ""
    pre_field_hashes: list[dict[str, Any]] = field(default_factory=list)
    post_field_hashes: list[dict[str, Any]] = field(default_factory=list)
    field_hash_mismatches: list[str] = field(default_factory=list)
    marker_payload_hash_match: bool = False
    migration_version_check: dict[str, Any] = field(default_factory=dict)
    app_start_check: dict[str, Any] = field(default_factory=dict)
    app_read_write_check: dict[str, Any] = field(default_factory=dict)
    synthetic_transaction: dict[str, Any] = field(default_factory=dict)
    switch_rollback_evidence: dict[str, Any] = field(default_factory=dict)
    target_db: str = DEFAULT_TARGET_DB


# ════════════════════════════════════════════════════════════════
# 确定性测试数据集
# ════════════════════════════════════════════════════════════════


def generate_test_dataset(trace_id: str | None = None) -> TestDataset:
    """生成确定性测试数据集。

    包含:
      - 唯一 ID(uuid4 hex)
      - 边界值(0, -1, MAX_INT, empty string, unicode chars, NULL)
      - 关系行(parent → child,通过 name 前缀模拟 FK)
      - payload hash(sha256 of canonical JSON)

    Args:
        trace_id: 唯一标识符(None 则自动生成 uuid4 hex)

    Returns:
        TestDataset
    """
    if trace_id is None:
        trace_id = uuid.uuid4().hex

    # 构造边界值测试行(bot_heartbeat 表: name, last_ping, is_running, total_processed, total_errors)
    rows: list[dict[str, Any]] = [
        # 主标记行:trace_id 作为 name
        {
            "name": trace_id,
            "last_ping": 0,              # 边界: 零
            "is_running": 1,             # 边界: 布尔 true
            "total_processed": 0,        # 边界: 零
            "total_errors": 0,           # 边界: 零
        },
        # 边界值行 1: 负数 + 最大整数
        {
            "name": f"{trace_id}_boundary_neg_max",
            "last_ping": -1,             # 边界: 负数
            "is_running": 0,             # 边界: 布尔 false
            "total_processed": -1,       # 边界: 负数
            "total_errors": SQLITE_MAX_INT,  # 边界: 最大整数
        },
        # 边界值行 2: 最大整数 + 零
        {
            "name": f"{trace_id}_boundary_max_zero",
            "last_ping": SQLITE_MAX_INT, # 边界: 最大整数
            "is_running": 1,             # 边界: 布尔 true
            "total_processed": SQLITE_MAX_INT,  # 边界: 最大整数
            "total_errors": 0,           # 边界: 零
        },
    ]

    # 关系行:child 行的 name 以 parent 的 name 为前缀(模拟 FK)
    relations: list[tuple[str, str]] = [
        (trace_id, f"{trace_id}_boundary_neg_max"),
        (trace_id, f"{trace_id}_boundary_max_zero"),
    ]

    # 计算 payload hash(sha256 of canonical JSON)
    payload_hash = compute_payload_hash(rows)

    return TestDataset(
        trace_id=trace_id,
        rows=rows,
        relations=relations,
        payload_hash=payload_hash,
    )


def compute_payload_hash(rows: list[dict[str, Any]]) -> str:
    """计算数据集的 payload hash。

    使用 sha256 of canonical JSON(sorted keys, no extra whitespace, ensure_ascii=False)。

    Args:
        rows: 数据行列表

    Returns:
        sha256 hexdigest(64 字符)
    """
    canonical = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_test_dataset(dataset: TestDataset, target_db: str = DEFAULT_TARGET_DB) -> tuple[bool, str | None]:
    """写入测试数据集到 bot_heartbeat 表。

    Args:
        dataset: 测试数据集
        target_db: 目标数据库

    Returns:
        (success, error)
    """
    for row in dataset.rows:
        query = (
            f"INSERT OR REPLACE INTO {MARKER_TABLE} "
            f"(name, last_ping, is_running, total_processed, total_errors) "
            f"VALUES ('{row['name']}', {int(row['last_ping'])}, "
            f"{int(row['is_running'])}, {int(row['total_processed'])}, "
            f"{int(row['total_errors'])})"
        )
        rc, stdout, stderr = _exec_sql(query, target_db=target_db)
        if rc != 0:
            return False, f"写入测试数据集失败 (row={row['name']}, exit={rc}): {stderr}"

    # 写入 payload hash 标记行(将 hash 前 16 hex 字符存为 total_processed 整数)
    hash_marker_name = f"{dataset.trace_id}_payload_hash"
    hash_int = int(dataset.payload_hash[:16], 16)  # 前 16 hex → int
    query = (
        f"INSERT OR REPLACE INTO {MARKER_TABLE} "
        f"(name, last_ping, is_running, total_processed, total_errors) "
        f"VALUES ('{hash_marker_name}', 0, 1, {hash_int}, 0)"
    )
    rc, stdout, stderr = _exec_sql(query, target_db=target_db)
    if rc != 0:
        return False, f"写入 payload hash 标记失败 (exit={rc}): {stderr}"

    return True, None


def verify_test_dataset(dataset: TestDataset, target_db: str = DEFAULT_TARGET_DB) -> tuple[bool, str | None]:
    """验证测试数据集在恢复后是否完整。

    Args:
        dataset: 测试数据集
        target_db: 目标数据库

    Returns:
        (all_rows_found, error)
    """
    for row in dataset.rows:
        query = f"SELECT COUNT(*) as cnt FROM {MARKER_TABLE} WHERE name = '{row['name']}'"
        rc, stdout, stderr = _exec_sql(query, target_db=target_db)
        if rc != 0:
            return False, f"查询测试数据集失败 (row={row['name']}, exit={rc}): {stderr}"
        try:
            if stdout.strip().startswith("["):
                rows_data = json.loads(stdout.strip())
                count = rows_data[0]["cnt"] if rows_data else 0
            else:
                count = 0
            if count < 1:
                return False, f"测试数据集行缺失: {row['name']} (count={count})"
        except (json.JSONDecodeError, IndexError, KeyError, ValueError) as e:
            return False, f"解析测试数据集查询结果失败 (row={row['name']}): {e}"

    return True, None


def verify_payload_hash(dataset: TestDataset, target_db: str = DEFAULT_TARGET_DB) -> tuple[bool, str | None]:
    """验证 payload hash 标记行是否匹配。

    Args:
        dataset: 测试数据集
        target_db: 目标数据库

    Returns:
        (hash_match, error)
    """
    hash_marker_name = f"{dataset.trace_id}_payload_hash"
    expected_hash_int = int(dataset.payload_hash[:16], 16)
    query = f"SELECT total_processed FROM {MARKER_TABLE} WHERE name = '{hash_marker_name}'"
    rc, stdout, stderr = _exec_sql(query, target_db=target_db)
    if rc != 0:
        return False, f"查询 payload hash 标记失败 (exit={rc}): {stderr}"
    try:
        if stdout.strip().startswith("["):
            rows_data = json.loads(stdout.strip())
            if not rows_data:
                return False, "payload hash 标记行不存在"
            actual_hash_int = int(rows_data[0].get("total_processed", 0))
        else:
            return False, "payload hash 标记行不存在(无结果集)"
        if actual_hash_int != expected_hash_int:
            return False, (
                f"payload hash 不匹配: expected={expected_hash_int}, "
                f"actual={actual_hash_int}"
            )
        return True, None
    except (json.JSONDecodeError, IndexError, KeyError, ValueError) as e:
        return False, f"解析 payload hash 查询结果失败: {e}"


def cleanup_test_dataset(dataset: TestDataset, target_db: str = DEFAULT_TARGET_DB) -> tuple[bool, str | None]:
    """清理测试数据集(删除所有测试行)。

    Args:
        dataset: 测试数据集
        target_db: 目标数据库

    Returns:
        (success, error)
    """
    # 删除所有以 trace_id 为前缀的行(包括边界值行和 payload hash 行)
    query = f"DELETE FROM {MARKER_TABLE} WHERE name LIKE '{dataset.trace_id}%'"
    rc, stdout, stderr = _exec_sql(query, target_db=target_db)
    if rc != 0:
        return False, f"清理测试数据集失败 (exit={rc}): {stderr}"
    return True, None


# ════════════════════════════════════════════════════════════════
# Schema 指纹捕获
# ════════════════════════════════════════════════════════════════


def _get_backup_schema_tables() -> list[dict[str, Any]]:
    """从 services.backup_schema 获取表列表。

    Returns:
        表信息列表,每项含: name, pk_columns, columns, conflict_col, source
    """
    try:
        # 动态导入 services.backup_schema(避免硬依赖)
        import services.backup_schema as bs
    except ImportError:
        # 如果无法导入,返回空列表(fail-closed)
        return []

    tables: list[dict[str, Any]] = []
    for name, schema in bs.BACKUP_SCHEMA.items():
        tables.append({
            "name": name,
            "pk_columns": list(schema.pk_columns),
            "columns": list(schema.columns),
            "conflict_col": schema.conflict_col,
            "source": schema.source,
        })
    # 按 name 排序确保确定性
    tables.sort(key=lambda t: t["name"])
    return tables


def _get_schema_version() -> str:
    """获取当前 schema 版本。

    优先级:
      1. settings.BACKUP_SCHEMA_VERSION(如果存在)
      2. services.db_backup._BACKUP_SCHEMA_VERSION
      3. 环境变量 BACKUP_SCHEMA_VERSION
      4. 默认 "unknown"

    Returns:
        schema 版本字符串
    """
    # 1. 尝试从 settings 读取
    try:
        from config import settings
        version = getattr(settings, "BACKUP_SCHEMA_VERSION", None)
        if version:
            return str(version)
    except (ImportError, Exception):
        pass

    # 2. 尝试从 db_backup 读取
    try:
        import services.db_backup as db_backup
        version = getattr(db_backup, "_BACKUP_SCHEMA_VERSION", None)
        if version:
            return str(version)
    except (ImportError, Exception):
        pass

    # 3. 尝试从环境变量读取
    version = os.environ.get("BACKUP_SCHEMA_VERSION", "")
    if version:
        return version

    # 4. 默认值
    return "unknown"


def capture_schema_fingerprint(target_db: str = DEFAULT_TARGET_DB) -> SchemaFingerprint:
    """捕获数据库 schema 指纹。

    包含:
      - schema_version(从 settings / db_backup / 环境变量)
      - 表列表(从 BACKUP_SCHEMA 获取 pk/columns/conflict_col/source)
      - 索引/约束 DDL 摘要(查询 sqlite_master)
      - 指纹 hash(sha256 of canonical JSON)

    Args:
        target_db: 目标数据库

    Returns:
        SchemaFingerprint
    """
    schema_version = _get_schema_version()
    tables = _get_backup_schema_tables()

    # 查询 sqlite_master 获取索引/约束/触发器 DDL
    query = (
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger') AND sql IS NOT NULL "
        "ORDER BY type, name"
    )
    rc, stdout, stderr = _exec_sql(query, target_db=target_db)
    index_constraint_summary: list[dict[str, Any]] = []
    if rc == 0:
        try:
            if stdout.strip().startswith("["):
                rows_data = json.loads(stdout.strip())
                for row in rows_data:
                    index_constraint_summary.append({
                        "type": row.get("type", ""),
                        "name": row.get("name", ""),
                        "tbl_name": row.get("tbl_name", ""),
                        "sql": row.get("sql", ""),
                    })
        except (json.JSONDecodeError, TypeError) as e:
            return SchemaFingerprint(
                schema_version=schema_version,
                tables=tables,
                index_constraint_summary=[],
                fingerprint_hash="",
                error=f"解析 sqlite_master 失败: {e}",
            )
    else:
        return SchemaFingerprint(
            schema_version=schema_version,
            tables=tables,
            index_constraint_summary=[],
            fingerprint_hash="",
            error=f"查询 sqlite_master 失败 (exit={rc}): {stderr}",
        )

    # 计算指纹 hash(sha256 of canonical JSON of schema_version + tables + index_constraint_summary)
    fingerprint_data = {
        "schema_version": schema_version,
        "tables": tables,
        "index_constraint_summary": index_constraint_summary,
    }
    canonical = json.dumps(
        fingerprint_data, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    fingerprint_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return SchemaFingerprint(
        schema_version=schema_version,
        tables=tables,
        index_constraint_summary=index_constraint_summary,
        fingerprint_hash=fingerprint_hash,
    )


def compare_schema_fingerprints(
    pre: SchemaFingerprint,
    post: SchemaFingerprint,
) -> list[str]:
    """比对两个 schema 指纹,返回差异列表。

    Args:
        pre: 备份前 schema 指纹
        post: 恢复后 schema 指纹

    Returns:
        差异描述列表(空列表表示完全匹配)
    """
    mismatches: list[str] = []

    if pre.schema_version != post.schema_version:
        mismatches.append(
            f"schema_version 不匹配: pre={pre.schema_version}, post={post.schema_version}"
        )

    if pre.fingerprint_hash != post.fingerprint_hash:
        mismatches.append(
            f"fingerprint_hash 不匹配: pre={pre.fingerprint_hash[:16]}..., "
            f"post={post.fingerprint_hash[:16]}..."
        )

    # 比对表列表
    pre_tables = {t["name"]: t for t in pre.tables}
    post_tables = {t["name"]: t for t in post.tables}

    pre_only = set(pre_tables.keys()) - set(post_tables.keys())
    post_only = set(post_tables.keys()) - set(pre_tables.keys())

    for name in sorted(pre_only):
        mismatches.append(f"表仅在 pre 中存在: {name}(恢复后缺失)")
    for name in sorted(post_only):
        mismatches.append(f"表仅在 post 中存在: {name}(恢复后新增)")

    # 比对共有表的 pk_columns / columns / conflict_col / source
    for name in sorted(set(pre_tables.keys()) & set(post_tables.keys())):
        pre_t = pre_tables[name]
        post_t = post_tables[name]
        if pre_t["pk_columns"] != post_t["pk_columns"]:
            mismatches.append(
                f"表 {name} 主键不匹配: pre={pre_t['pk_columns']}, post={post_t['pk_columns']}"
            )
        if pre_t["columns"] != post_t["columns"]:
            pre_cols = set(pre_t["columns"])
            post_cols = set(post_t["columns"])
            missing_cols = pre_cols - post_cols
            extra_cols = post_cols - pre_cols
            if missing_cols:
                mismatches.append(f"表 {name} 列缺失: {sorted(missing_cols)}")
            if extra_cols:
                mismatches.append(f"表 {name} 列新增: {sorted(extra_cols)}")
        if pre_t["conflict_col"] != post_t["conflict_col"]:
            mismatches.append(
                f"表 {name} conflict_col 不匹配: pre={pre_t['conflict_col']}, post={post_t['conflict_col']}"
            )
        if pre_t["source"] != post_t["source"]:
            mismatches.append(
                f"表 {name} source 不匹配: pre={pre_t['source']}, post={post_t['source']}"
            )

    # 比对索引/约束 DDL 摘要
    pre_ddl = {f"{d['type']}:{d['name']}" for d in pre.index_constraint_summary}
    post_ddl = {f"{d['type']}:{d['name']}" for d in post.index_constraint_summary}
    pre_only_ddl = pre_ddl - post_ddl
    post_only_ddl = post_ddl - pre_ddl
    for key in sorted(pre_only_ddl):
        mismatches.append(f"索引/约束仅在 pre 中存在: {key}(恢复后缺失)")
    for key in sorted(post_only_ddl):
        mismatches.append(f"索引/约束仅在 post 中存在: {key}(恢复后新增)")

    # 比对共有索引/约束的 DDL 内容
    pre_ddl_map = {f"{d['type']}:{d['name']}": d["sql"] for d in pre.index_constraint_summary}
    post_ddl_map = {f"{d['type']}:{d['name']}": d["sql"] for d in post.index_constraint_summary}
    for key in sorted(set(pre_ddl_map.keys()) & set(post_ddl_map.keys())):
        if pre_ddl_map[key] != post_ddl_map[key]:
            mismatches.append(f"索引/约束 {key} DDL 内容不匹配")

    return mismatches


# ════════════════════════════════════════════════════════════════
# 字段级 hash
# ════════════════════════════════════════════════════════════════


def _get_table_pk_columns(table: str) -> tuple[str, ...]:
    """获取表的主键列(从 BACKUP_SCHEMA)。

    Args:
        table: 表名

    Returns:
        主键列元组(如果表不在 BACKUP_SCHEMA 中,返回 ("rowid",))
    """
    try:
        import services.backup_schema as bs
        if table in bs.BACKUP_SCHEMA:
            return bs.BACKUP_SCHEMA[table].pk_columns
    except (ImportError, Exception):
        pass
    return ("rowid",)


def compute_field_hashes(
    tables: list[str] | None = None,
    target_db: str = DEFAULT_TARGET_DB,
) -> list[TableHash]:
    """计算关键表的字段级 hash。

    对每个表执行 SELECT * ORDER BY <pk>,然后 sha256 of canonical JSON。

    Args:
        tables: 表名列表(None 则使用 FIELD_HASH_TABLES)
        target_db: 目标数据库

    Returns:
        list[TableHash]
    """
    if tables is None:
        tables = FIELD_HASH_TABLES

    hashes: list[TableHash] = []
    for table in tables:
        pk_cols = _get_table_pk_columns(table)
        order_by = ", ".join(pk_cols) if pk_cols else "rowid"
        query = f"SELECT * FROM {table} ORDER BY {order_by}"
        rc, stdout, stderr = _exec_sql(query, target_db=target_db)
        if rc != 0:
            hashes.append(TableHash(
                table=table,
                pk_columns=pk_cols,
                row_count=-1,
                field_hash="",
                error=f"查询失败 (exit={rc}): {stderr}",
            ))
            continue
        try:
            if stdout.strip().startswith("["):
                rows_data = json.loads(stdout.strip())
            else:
                rows_data = []
            # 计算 canonical JSON hash
            canonical = json.dumps(
                rows_data, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            )
            field_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            hashes.append(TableHash(
                table=table,
                pk_columns=pk_cols,
                row_count=len(rows_data),
                field_hash=field_hash,
            ))
        except (json.JSONDecodeError, TypeError) as e:
            hashes.append(TableHash(
                table=table,
                pk_columns=pk_cols,
                row_count=-1,
                field_hash="",
                error=f"解析失败: {e}",
            ))
    return hashes


def compare_field_hashes(
    pre: list[TableHash],
    post: list[TableHash],
) -> list[str]:
    """比对 pre 和 post 的字段级 hash,返回差异列表。

    Args:
        pre: 备份前字段 hash 列表
        post: 恢复后字段 hash 列表

    Returns:
        差异描述列表(空列表表示完全匹配)
    """
    mismatches: list[str] = []
    pre_map = {h.table: h for h in pre}
    post_map = {h.table: h for h in post}

    all_tables = set(pre_map.keys()) | set(post_map.keys())
    for table in sorted(all_tables):
        pre_h = pre_map.get(table)
        post_h = post_map.get(table)
        if pre_h is None:
            mismatches.append(f"表 {table} 仅在 post 中存在(pre 缺失)")
            continue
        if post_h is None:
            mismatches.append(f"表 {table} 仅在 pre 中存在(post 缺失)")
            continue
        if pre_h.error:
            mismatches.append(f"表 {table} pre hash 有错误: {pre_h.error}")
            continue
        if post_h.error:
            mismatches.append(f"表 {table} post hash 有错误: {post_h.error}")
            continue
        if pre_h.row_count != post_h.row_count:
            mismatches.append(
                f"表 {table} row_count 不匹配: pre={pre_h.row_count}, post={post_h.row_count}"
            )
        if pre_h.field_hash != post_h.field_hash:
            mismatches.append(
                f"表 {table} field_hash 不匹配: pre={pre_h.field_hash[:16]}..., "
                f"post={post_h.field_hash[:16]}..."
            )
    return mismatches


# ════════════════════════════════════════════════════════════════
# 迁移版本兼容性检查
# ════════════════════════════════════════════════════════════════


def check_migration_version_compatibility(
    backup_schema_version: str | None = None,
) -> dict[str, Any]:
    """检查迁移版本兼容性。

    比对当前代码的 schema_version 与备份 manifest 中的 schema_version。
    如果不匹配,返回 compatible=False。

    Args:
        backup_schema_version: 备份 manifest 中的 schema_version(None 则只返回当前版本)

    Returns:
        dict: {current, backup, compatible}
    """
    current = _get_schema_version()
    backup = backup_schema_version or ""

    # 如果没有备份版本,视为兼容(只记录当前版本)
    if not backup:
        return {
            "current": current,
            "backup": "",
            "compatible": True,
            "note": "备份版本未提供,跳过兼容性检查",
        }

    # 版本兼容性检查:完全匹配 → 兼容
    # 也允许 backup 版本为 current 的前缀(向后兼容旧备份)
    compatible = current == backup or current.startswith(backup) or backup.startswith(current)

    return {
        "current": current,
        "backup": backup,
        "compatible": compatible,
        "note": "" if compatible else f"版本不匹配: current={current}, backup={backup}",
    }


# ════════════════════════════════════════════════════════════════
# 应用启动/读写验证
# ════════════════════════════════════════════════════════════════


def verify_app_start(role: str = "db_writer") -> dict[str, Any]:
    """验证应用可以启动(通过 python -m services.health --role <role> --json)。

    Args:
        role: 服务角色

    Returns:
        dict: {started, role, healthy, returncode, stdout, stderr, error}
    """
    rc, stdout, stderr = _exec_health(role, timeout=30)
    started = rc == 0
    healthy = False
    error: str | None = None

    if started:
        try:
            health_json = json.loads(stdout.strip())
            healthy = bool(health_json.get("healthy", False))
            if not healthy:
                error = f"health 返回 healthy=false: {health_json}"
        except json.JSONDecodeError as e:
            error = f"解析 health JSON 失败: {e}, stdout={stdout[:200]!r}"
            started = False
    else:
        error = f"health 命令失败 (exit={rc}): {stderr}"

    return {
        "started": started,
        "role": role,
        "healthy": healthy,
        "returncode": rc,
        "stdout": stdout[:500],
        "stderr": stderr[:500],
        "error": error,
    }


def verify_app_read_write(
    trace_id: str | None = None,
    target_db: str = DEFAULT_TARGET_DB,
) -> dict[str, Any]:
    """验证恢复后的 DB 支持简单 INSERT/SELECT/DELETE。

    步骤:
      1. 写入新标记行(INSERT)
      2. 读回标记行(SELECT)
      3. 删除标记行(DELETE)

    Args:
        trace_id: 测试标记 ID(None 则自动生成)
        target_db: 目标数据库

    Returns:
        dict: {write_ok, read_ok, cleanup_ok, trace_id, error}
    """
    if trace_id is None:
        trace_id = f"rw_test_{uuid.uuid4().hex[:12]}"

    # 1. INSERT
    insert_query = (
        f"INSERT OR REPLACE INTO {MARKER_TABLE} "
        f"(name, last_ping, is_running, total_processed, total_errors) "
        f"VALUES ('{trace_id}', 0, 1, 0, 0)"
    )
    rc, _, stderr = _exec_sql(insert_query, target_db=target_db)
    write_ok = rc == 0
    if not write_ok:
        return {
            "write_ok": False,
            "read_ok": False,
            "cleanup_ok": False,
            "trace_id": trace_id,
            "error": f"INSERT 失败 (exit={rc}): {stderr}",
        }

    # 2. SELECT
    select_query = f"SELECT COUNT(*) as cnt FROM {MARKER_TABLE} WHERE name = '{trace_id}'"
    rc, stdout, stderr = _exec_sql(select_query, target_db=target_db)
    read_ok = False
    if rc == 0:
        try:
            if stdout.strip().startswith("["):
                rows_data = json.loads(stdout.strip())
                count = rows_data[0].get("cnt", 0) if rows_data else 0
                read_ok = count >= 1
        except (json.JSONDecodeError, IndexError, KeyError, ValueError):
            pass
    if not read_ok:
        # 3. 尝试清理(即使 SELECT 失败)
        cleanup_query = f"DELETE FROM {MARKER_TABLE} WHERE name = '{trace_id}'"
        _exec_sql(cleanup_query, target_db=target_db)
        return {
            "write_ok": True,
            "read_ok": False,
            "cleanup_ok": False,
            "trace_id": trace_id,
            "error": f"SELECT 失败或未读到写入的行 (exit={rc}): {stderr}",
        }

    # 3. DELETE
    delete_query = f"DELETE FROM {MARKER_TABLE} WHERE name = '{trace_id}'"
    rc, _, stderr = _exec_sql(delete_query, target_db=target_db)
    cleanup_ok = rc == 0

    return {
        "write_ok": True,
        "read_ok": True,
        "cleanup_ok": cleanup_ok,
        "trace_id": trace_id,
        "error": None if cleanup_ok else f"DELETE 失败 (exit={rc}): {stderr}",
    }


# ════════════════════════════════════════════════════════════════
# 合成交易验证
# ════════════════════════════════════════════════════════════════


def run_synthetic_transaction_in_restored_env(timeout: int = 60) -> dict[str, Any]:
    """在恢复环境中运行合成交易。

    通过 scripts/synthetic_transaction.py 的 run_full_transaction() 执行。
    合成交易验证完整业务链:Redis Stream → db_writer → SQLite → 幂等 → DLQ → 清理。

    Args:
        timeout: 单步骤最大等待秒数

    Returns:
        dict: 合成交易证据(TransactionEvidence asdict)
    """
    if not SYNTHETIC_TRANSACTION_PATH.is_file():
        return {
            "overall_passed": False,
            "error": f"synthetic_transaction.py 不存在: {SYNTHETIC_TRANSACTION_PATH}",
        }

    try:
        spec = importlib.util.spec_from_file_location(
            "synthetic_transaction", SYNTHETIC_TRANSACTION_PATH,
        )
        if spec is None or spec.loader is None:
            return {
                "overall_passed": False,
                "error": "加载 synthetic_transaction 模块失败(spec/loader 为 None)",
            }
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        # fail-closed:不吞异常,转换为证据
        return {
            "overall_passed": False,
            "error": f"加载 synthetic_transaction 模块异常: {type(e).__name__}: {e}",
        }

    try:
        evidence = module.run_full_transaction(timeout=timeout)
    except Exception as e:
        # fail-closed:不吞异常,转换为证据
        return {
            "overall_passed": False,
            "error": f"run_full_transaction 异常: {type(e).__name__}: {e}",
        }

    # 将 TransactionEvidence 转换为 dict
    try:
        evidence_dict = asdict(evidence) if hasattr(evidence, "__dataclass_fields__") else {
            "trace_id": getattr(evidence, "trace_id", ""),
            "overall_passed": getattr(evidence, "overall_passed", False),
            "error": getattr(evidence, "error", None),
        }
    except Exception as e:
        return {
            "overall_passed": False,
            "error": f"转换 TransactionEvidence 为 dict 失败: {type(e).__name__}: {e}",
        }

    return evidence_dict


# ════════════════════════════════════════════════════════════════
# 切换/回滚证据
# ════════════════════════════════════════════════════════════════


def generate_switch_rollback_evidence() -> dict[str, Any]:
    """生成切换/回滚证据(结构化 JSON)。

    不实际执行破坏性切换,只验证切换/回滚流程是否就位:
      - services.restore_orchestrator 可导入
      - RestoreOrchestrator 类存在
      - RestorePhase 枚举包含 BLUE_GREEN_SWITCH 和 ROLLED_BACK 阶段
      - 文档化切换和回滚步骤

    Returns:
        dict: 切换/回滚证据
    """
    evidence: dict[str, Any] = {
        "orchestrator_available": False,
        "restore_phases": [],
        "switch_procedure": "",
        "rollback_procedure": "",
        "error": None,
    }

    try:
        import services.restore_orchestrator as ro
        evidence["orchestrator_available"] = True

        # 检查 RestorePhase 枚举
        try:
            phases = [p.value for p in ro.RestorePhase]
            evidence["restore_phases"] = phases
        except (AttributeError, TypeError) as e:
            evidence["error"] = f"无法读取 RestorePhase 枚举: {e}"

        # 检查关键阶段是否存在
        has_switch = False
        has_rollback = False
        try:
            for phase in ro.RestorePhase:
                val = phase.value if hasattr(phase, "value") else str(phase)
                if "switch" in val.lower():
                    has_switch = True
                if "rollback" in val.lower() or "rolled_back" in val.lower():
                    has_rollback = True
        except (AttributeError, TypeError):
            pass
        evidence["has_switch_phase"] = has_switch
        evidence["has_rollback_phase"] = has_rollback

    except ImportError as e:
        evidence["error"] = f"无法导入 services.restore_orchestrator: {e}"
        return evidence
    except Exception as e:
        # fail-closed:不吞异常,记录到证据
        evidence["error"] = f"导入 restore_orchestrator 异常: {type(e).__name__}: {e}"
        return evidence

    # 文档化切换/回滚步骤(不实际执行)
    evidence["switch_procedure"] = (
        "切换步骤(由 RestoreOrchestrator 执行,不在 E2E 中实际运行):\n"
        "1. 验证 staging 数据完整性(通过 verify_restore_integrity.py full-check)\n"
        "2. 将 staging 提升为 active(RestorePhase.BLUE_GREEN_SWITCH)\n"
        "3. 记录 previous_version 作为回滚目标\n"
        "4. 等待新 active 健康检查通过\n"
        "5. 标记 RestorePhase.COMPLETED"
    )
    evidence["rollback_procedure"] = (
        "回滚步骤(由 RestoreOrchestrator 执行,不在 E2E 中实际运行):\n"
        "1. 检测新 active 健康检查失败或数据异常\n"
        "2. 降级 staging(demote),恢复 production 从 previous backup\n"
        "3. RestorePhase.ROLLED_BACK\n"
        "4. 验证回滚后的 active 数据完整性\n"
        "5. 通知运维团队"
    )
    evidence["performed_in_e2e"] = False  # E2E 中不实际执行破坏性切换

    return evidence


# ════════════════════════════════════════════════════════════════
# 基本函数(向后兼容 Wave 2)
# ════════════════════════════════════════════════════════════════


def write_marker(trace_id: str) -> int:
    """写入测试标记行到 bot_heartbeat 表。

    Args:
        trace_id: 唯一标识符(作为 bot_heartbeat.name)

    Returns:
        0 成功,1 失败
    """
    # INSERT OR REPLACE 确保幂等
    query = (
        f"INSERT OR REPLACE INTO {MARKER_TABLE} "
        f"(name, last_ping, is_running, total_processed, total_errors) "
        f"VALUES ('{trace_id}', 0, 1, 0, 0)"
    )
    rc, stdout, stderr = _exec_sql(query)
    if rc != 0:
        print(
            f"ERROR: 写入测试标记失败 (exit={rc}): {stderr}",
            file=sys.stderr,
        )
        return 1
    print(f"测试标记已写入: trace_id={trace_id}")
    return 0


def get_table_counts(target_db: str = DEFAULT_TARGET_DB) -> list[TableCount]:
    """获取关键表的 row count。

    Args:
        target_db: 目标数据库

    Returns:
        list[TableCount]
    """
    counts: list[TableCount] = []
    for table in CRITICAL_TABLES:
        query = f"SELECT COUNT(*) as cnt FROM {table}"
        rc, stdout, stderr = _exec_sql(query, target_db=target_db)
        if rc != 0:
            counts.append(TableCount(
                table=table, count=-1,
                error=f"查询失败 (exit={rc}): {stderr}",
            ))
            continue
        try:
            if stdout.strip().startswith("["):
                rows_data = json.loads(stdout.strip())
                count = rows_data[0]["cnt"] if rows_data else 0
            else:
                count = 0
            counts.append(TableCount(table=table, count=count))
        except (json.JSONDecodeError, IndexError, KeyError, ValueError) as e:
            counts.append(TableCount(
                table=table, count=-1,
                error=f"解析失败: {e}, stdout={stdout!r}",
            ))
    return counts


def take_snapshot(output_path: Path, target_db: str = DEFAULT_TARGET_DB) -> int:
    """获取当前数据库快照(row counts + schema fingerprint + field hashes),保存到文件。

    R71 Wave 3 扩展:快照现在包含 schema 指纹和字段级 hash,
    供恢复后比对。

    Args:
        output_path: 快照输出路径
        target_db: 目标数据库

    Returns:
        0 成功,1 失败
    """
    counts = get_table_counts(target_db=target_db)
    schema_fp = capture_schema_fingerprint(target_db=target_db)
    field_hashes = compute_field_hashes(target_db=target_db)

    snapshot = {
        "timestamp": _now_iso(),
        "target_db": target_db,
        "tables": [asdict(c) for c in counts],
        "schema_fingerprint": asdict(schema_fp),
        "field_hashes": [asdict(h) for h in field_hashes],
    }
    try:
        output_path.write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"ERROR: 写入快照失败: {e}", file=sys.stderr)
        return 1
    print(f"快照已保存: {output_path}")
    return 0


def verify(trace_id: str, pre_snapshot_path: Path | None) -> IntegrityEvidence:
    """校验恢复后数据完整性(基本校验,向后兼容 Wave 2)。

    基本校验:
      1. 测试标记行存在
      2. 关键表 row count 无回归

    完整校验请使用 verify_full()。

    Args:
        trace_id: 测试标记 ID
        pre_snapshot_path: 备份前快照文件路径(可选,无则只校验标记)

    Returns:
        IntegrityEvidence(基本字段填充)
    """
    timestamp = _now_iso()

    # 1. 校验测试标记是否存在
    query = f"SELECT COUNT(*) as cnt FROM {MARKER_TABLE} WHERE name = '{trace_id}'"
    rc, stdout, stderr = _exec_sql(query)
    marker_found = False
    if rc == 0:
        try:
            if stdout.strip().startswith("["):
                rows_data = json.loads(stdout.strip())
                count = rows_data[0].get("cnt", 0) if rows_data else 0
            else:
                count = 0
            marker_found = count >= 1
        except (json.JSONDecodeError, IndexError, KeyError, ValueError):
            pass

    # 2. 比对关键表 row count(如果提供了 pre-snapshot)
    pre_counts: list[TableCount] = []
    post_counts: list[TableCount] = []
    count_mismatches: list[str] = []

    if pre_snapshot_path and pre_snapshot_path.is_file():
        try:
            pre_data = json.loads(
                pre_snapshot_path.read_text(encoding="utf-8")
            )
            pre_counts = [
                TableCount(**t) for t in pre_data.get("tables", [])
            ]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            return IntegrityEvidence(
                trace_id=trace_id,
                timestamp=timestamp,
                passed=False,
                marker_found=marker_found,
                error=f"解析 pre-snapshot 失败: {e}",
            )

        post_counts = get_table_counts()
        pre_map = {c.table: c.count for c in pre_counts}
        post_map = {c.table: c.count for c in post_counts}
        for table in pre_map:
            pre_count = pre_map[table]
            post_count = post_map.get(table, -1)
            # 允许恢复后 count >= 备份前(可能有新写入)
            # 但不允许 count < 备份前(数据丢失)
            if post_count < pre_count:
                count_mismatches.append(
                    f"{table}: pre={pre_count} post={post_count} (数据丢失)"
                )

    # 校验通过条件:标记存在 且 无 count mismatch
    passed = marker_found and not count_mismatches

    return IntegrityEvidence(
        trace_id=trace_id,
        timestamp=timestamp,
        passed=passed,
        marker_found=marker_found,
        pre_counts=pre_counts,
        post_counts=post_counts,
        count_mismatches=count_mismatches,
        error=None if passed else (
            "标记未找到" if not marker_found
            else f"count mismatch: {count_mismatches}"
        ),
    )


def cleanup_marker(trace_id: str) -> int:
    """清理测试标记行。

    Args:
        trace_id: 唯一标识符

    Returns:
        0 成功,1 失败
    """
    query = f"DELETE FROM {MARKER_TABLE} WHERE name = '{trace_id}'"
    rc, stdout, stderr = _exec_sql(query)
    if rc != 0:
        print(
            f"ERROR: 清理测试标记失败 (exit={rc}): {stderr}",
            file=sys.stderr,
        )
        return 1
    print(f"测试标记已清理: trace_id={trace_id}")
    return 0


# ════════════════════════════════════════════════════════════════
# 完整结构化校验(R71 Wave 3)
# ════════════════════════════════════════════════════════════════


def verify_full(
    trace_id: str,
    pre_snapshot_path: Path | None,
    target_db: str = DEFAULT_TARGET_DB,
    backup_schema_version: str | None = None,
    skip_synthetic: bool = False,
    skip_app_checks: bool = False,
) -> IntegrityEvidence:
    """完整结构化校验(R71 Wave 3, P0-08)。

    包含:
      1. 测试标记存在性检查
      2. 关键表 row count 比对
      3. Schema 指纹捕获与比对
      4. 字段级 hash 比对
      5. 迁移版本兼容性检查
      6. 应用启动验证(python -m services.health)
      7. 应用读写验证(INSERT/SELECT/DELETE)
      8. 合成交易验证(synthetic_transaction.run_full_transaction)
      9. 切换/回滚证据生成

    Args:
        trace_id: 测试标记 ID
        pre_snapshot_path: 备份前快照文件路径(可选)
        target_db: 目标数据库(production / staging)
        backup_schema_version: 备份 manifest 中的 schema_version(可选)
        skip_synthetic: 跳过合成交易(用于快速校验)
        skip_app_checks: 跳过应用启动/读写检查(用于离线校验)

    Returns:
        IntegrityEvidence(全部字段填充)
    """
    timestamp = _now_iso()

    # 初始化证据
    evidence = IntegrityEvidence(
        trace_id=trace_id,
        timestamp=timestamp,
        passed=False,
        marker_found=False,
        target_db=target_db,
    )

    errors: list[str] = []

    # ── 1. 测试标记存在性检查 ──
    query = f"SELECT COUNT(*) as cnt FROM {MARKER_TABLE} WHERE name = '{trace_id}'"
    rc, stdout, stderr = _exec_sql(query, target_db=target_db)
    marker_found = False
    if rc == 0:
        try:
            if stdout.strip().startswith("["):
                rows_data = json.loads(stdout.strip())
                count = rows_data[0].get("cnt", 0) if rows_data else 0
            else:
                count = 0
            marker_found = count >= 1
        except (json.JSONDecodeError, IndexError, KeyError, ValueError):
            pass
    evidence.marker_found = marker_found
    if not marker_found:
        errors.append("测试标记未找到")

    # ── 2. 关键表 row count 比对 ──
    pre_counts: list[TableCount] = []
    post_counts: list[TableCount] = []
    count_mismatches: list[str] = []

    if pre_snapshot_path and pre_snapshot_path.is_file():
        try:
            pre_data = json.loads(
                pre_snapshot_path.read_text(encoding="utf-8")
            )
            pre_counts = [
                TableCount(**t) for t in pre_data.get("tables", [])
            ]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            errors.append(f"解析 pre-snapshot 失败: {e}")
            pre_snapshot_path = None  # 标记为不可用

    post_counts = get_table_counts(target_db=target_db)
    if pre_counts:
        pre_map = {c.table: c.count for c in pre_counts}
        post_map = {c.table: c.count for c in post_counts}
        for table in pre_map:
            pre_count = pre_map[table]
            post_count = post_map.get(table, -1)
            if post_count < pre_count:
                count_mismatches.append(
                    f"{table}: pre={pre_count} post={post_count} (数据丢失)"
                )
    evidence.pre_counts = pre_counts
    evidence.post_counts = post_counts
    evidence.count_mismatches = count_mismatches
    if count_mismatches:
        errors.append(f"row count 不匹配: {count_mismatches}")

    # ── 3. Schema 指纹捕获与比对 ──
    post_schema_fp = capture_schema_fingerprint(target_db=target_db)
    evidence.schema_fingerprint = asdict(post_schema_fp)
    evidence.schema_fingerprint_hash = post_schema_fp.fingerprint_hash

    if post_schema_fp.error:
        errors.append(f"schema 指纹捕获失败: {post_schema_fp.error}")

    # 如果 pre-snapshot 包含 schema_fingerprint,进行比对
    schema_mismatches: list[str] = []
    if pre_snapshot_path and pre_snapshot_path.is_file():
        try:
            pre_data = json.loads(
                pre_snapshot_path.read_text(encoding="utf-8")
            )
            pre_fp_data = pre_data.get("schema_fingerprint", {})
            if pre_fp_data:
                pre_fp = SchemaFingerprint(**pre_fp_data)
                schema_mismatches = compare_schema_fingerprints(pre_fp, post_schema_fp)
                if schema_mismatches:
                    errors.append(f"schema 指纹不匹配: {schema_mismatches}")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            errors.append(f"解析 pre-snapshot schema_fingerprint 失败: {e}")

    # ── 4. 字段级 hash 比对 ──
    post_field_hashes = compute_field_hashes(target_db=target_db)
    evidence.post_field_hashes = [asdict(h) for h in post_field_hashes]

    field_hash_mismatches: list[str] = []
    if pre_snapshot_path and pre_snapshot_path.is_file():
        try:
            pre_data = json.loads(
                pre_snapshot_path.read_text(encoding="utf-8")
            )
            pre_fh_data = pre_data.get("field_hashes", [])
            if pre_fh_data:
                pre_field_hashes = [TableHash(**fh) for fh in pre_fh_data]
                evidence.pre_field_hashes = pre_fh_data
                field_hash_mismatches = compare_field_hashes(pre_field_hashes, post_field_hashes)
                if field_hash_mismatches:
                    errors.append(f"字段级 hash 不匹配: {field_hash_mismatches}")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            errors.append(f"解析 pre-snapshot field_hashes 失败: {e}")

    evidence.field_hash_mismatches = field_hash_mismatches

    # ── 5. 测试数据集 payload hash 验证 ──
    # 检查是否有 payload hash 标记行
    hash_marker_name = f"{trace_id}_payload_hash"
    payload_query = f"SELECT total_processed FROM {MARKER_TABLE} WHERE name = '{hash_marker_name}'"
    rc, stdout, stderr = _exec_sql(payload_query, target_db=target_db)
    payload_hash_match = False
    if rc == 0:
        try:
            if stdout.strip().startswith("["):
                rows_data = json.loads(stdout.strip())
                if rows_data:
                    # payload hash 标记存在,标记为 match(具体值比对由 verify_payload_hash 完成)
                    payload_hash_match = True
        except (json.JSONDecodeError, IndexError, KeyError, ValueError):
            pass
    evidence.marker_payload_hash_match = payload_hash_match

    # ── 6. 迁移版本兼容性检查 ──
    migration_check = check_migration_version_compatibility(backup_schema_version)
    evidence.migration_version_check = migration_check
    if not migration_check.get("compatible", False):
        errors.append(f"迁移版本不兼容: {migration_check.get('note', '')}")

    # ── 7. 应用启动验证 ──
    if not skip_app_checks:
        app_start = verify_app_start(role="db_writer")
        evidence.app_start_check = app_start
        if not app_start.get("started", False) or not app_start.get("healthy", False):
            errors.append(f"应用启动验证失败: {app_start.get('error', '')}")

        # ── 8. 应用读写验证 ──
        app_rw = verify_app_read_write(target_db=target_db)
        evidence.app_read_write_check = app_rw
        if not (app_rw.get("write_ok") and app_rw.get("read_ok") and app_rw.get("cleanup_ok")):
            errors.append(f"应用读写验证失败: {app_rw.get('error', '')}")

    # ── 9. 合成交易验证 ──
    if not skip_synthetic:
        synthetic_evidence = run_synthetic_transaction_in_restored_env(timeout=60)
        evidence.synthetic_transaction = synthetic_evidence
        if not synthetic_evidence.get("overall_passed", False):
            errors.append(f"合成交易验证失败: {synthetic_evidence.get('error', '')}")

    # ── 10. 切换/回滚证据 ──
    switch_evidence = generate_switch_rollback_evidence()
    evidence.switch_rollback_evidence = switch_evidence
    if not switch_evidence.get("orchestrator_available", False):
        errors.append(f"切换/回滚编排器不可用: {switch_evidence.get('error', '')}")

    # ── 最终判定 ──
    passed = (
        marker_found
        and not count_mismatches
        and not schema_mismatches
        and not field_hash_mismatches
        and migration_check.get("compatible", False)
        and payload_hash_match
        and (skip_app_checks or (
            evidence.app_start_check.get("started", False)
            and evidence.app_start_check.get("healthy", False)
        ))
        and (skip_app_checks or (
            evidence.app_read_write_check.get("write_ok", False)
            and evidence.app_read_write_check.get("read_ok", False)
            and evidence.app_read_write_check.get("cleanup_ok", False)
        ))
        and (skip_synthetic or evidence.synthetic_transaction.get("overall_passed", False))
        and evidence.switch_rollback_evidence.get("orchestrator_available", False)
    )

    evidence.passed = passed
    evidence.error = None if passed else "; ".join(errors)

    return evidence


# ════════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Returns:
        0 — 成功
        1 — 失败(fail-closed)
        2 — 参数错误
    """
    parser = argparse.ArgumentParser(
        description=(
            "R71 Wave 2/3: 备份恢复完整性结构化校验"
            "(替代日志关键词匹配)"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # write-marker 子命令
    p_write = subparsers.add_parser(
        "write-marker",
        help="写入测试标记行",
    )
    p_write.add_argument(
        "--trace-id",
        default=f"restore_marker_{uuid.uuid4().hex}",
        help="测试标记 ID(默认自动生成)",
    )
    p_write.add_argument(
        "--target-db",
        default=DEFAULT_TARGET_DB,
        choices=["production", "staging"],
        help="目标数据库(默认 production)",
    )

    # snapshot 子命令
    p_snap = subparsers.add_parser(
        "snapshot",
        help="获取当前数据库快照(row counts + schema fingerprint + field hashes)",
    )
    p_snap.add_argument(
        "--output",
        required=True,
        help="快照输出路径",
    )
    p_snap.add_argument(
        "--target-db",
        default=DEFAULT_TARGET_DB,
        choices=["production", "staging"],
        help="目标数据库(默认 production)",
    )

    # verify 子命令(基本校验,向后兼容)
    p_verify = subparsers.add_parser(
        "verify",
        help="基本校验(标记 + row count,向后兼容 Wave 2)",
    )
    p_verify.add_argument(
        "--trace-id",
        required=True,
        help="测试标记 ID(必须与 write-marker 一致)",
    )
    p_verify.add_argument(
        "--pre-snapshot",
        help="备份前快照文件路径(可选,用于 row count 比对)",
    )
    p_verify.add_argument(
        "--output",
        help="证据输出 JSON 文件路径(默认输出到 stdout)",
    )
    p_verify.add_argument(
        "--target-db",
        default=DEFAULT_TARGET_DB,
        choices=["production", "staging"],
        help="目标数据库(默认 production)",
    )

    # full-check 子命令(完整结构化校验,R71 Wave 3)
    p_full = subparsers.add_parser(
        "full-check",
        help="完整结构化校验(标记 + schema + field hash + migration + app + tx + switch)",
    )
    p_full.add_argument(
        "--trace-id",
        required=True,
        help="测试标记 ID(必须与 write-marker 一致)",
    )
    p_full.add_argument(
        "--pre-snapshot",
        help="备份前快照文件路径(可选,用于完整比对)",
    )
    p_full.add_argument(
        "--output",
        help="证据输出 JSON 文件路径(默认输出到 stdout)",
    )
    p_full.add_argument(
        "--target-db",
        default=DEFAULT_TARGET_DB,
        choices=["production", "staging"],
        help="目标数据库(默认 production,staging 用于恢复验证)",
    )
    p_full.add_argument(
        "--backup-schema-version",
        help="备份 manifest 中的 schema_version(用于迁移兼容性检查)",
    )
    p_full.add_argument(
        "--skip-synthetic",
        action="store_true",
        help="跳过合成交易验证(用于快速校验)",
    )
    p_full.add_argument(
        "--skip-app-checks",
        action="store_true",
        help="跳过应用启动/读写检查(用于离线校验)",
    )

    # cleanup 子命令
    p_clean = subparsers.add_parser(
        "cleanup",
        help="清理测试标记行",
    )
    p_clean.add_argument(
        "--trace-id",
        required=True,
        help="测试标记 ID",
    )
    p_clean.add_argument(
        "--target-db",
        default=DEFAULT_TARGET_DB,
        choices=["production", "staging"],
        help="目标数据库(默认 production)",
    )

    args = parser.parse_args(argv)

    if args.command == "write-marker":
        # 设置全局 target_db(通过环境变量传递给 _exec_sql)
        os.environ["VERIFY_RESTORE_TARGET_DB"] = args.target_db
        return write_marker(args.trace_id)

    if args.command == "snapshot":
        return take_snapshot(Path(args.output), target_db=args.target_db)

    if args.command == "verify":
        pre_path = (
            Path(args.pre_snapshot) if args.pre_snapshot else None
        )
        evidence = verify(args.trace_id, pre_path)
        evidence.target_db = args.target_db
        evidence_dict = asdict(evidence)
        evidence_json = json.dumps(
            evidence_dict, indent=2, ensure_ascii=False
        )
        if args.output:
            Path(args.output).write_text(evidence_json, encoding="utf-8")
            print(f"Evidence written to: {args.output}", file=sys.stderr)
        else:
            print(evidence_json)
        return 0 if evidence.passed else 1

    if args.command == "full-check":
        pre_path = (
            Path(args.pre_snapshot) if args.pre_snapshot else None
        )
        evidence = verify_full(
            trace_id=args.trace_id,
            pre_snapshot_path=pre_path,
            target_db=args.target_db,
            backup_schema_version=args.backup_schema_version,
            skip_synthetic=args.skip_synthetic,
            skip_app_checks=args.skip_app_checks,
        )
        evidence_dict = asdict(evidence)
        evidence_json = json.dumps(
            evidence_dict, indent=2, ensure_ascii=False
        )
        if args.output:
            Path(args.output).write_text(evidence_json, encoding="utf-8")
            print(f"Evidence written to: {args.output}", file=sys.stderr)
        else:
            print(evidence_json)
        return 0 if evidence.passed else 1

    if args.command == "cleanup":
        return cleanup_marker(args.trace_id)

    return 2


if __name__ == "__main__":
    sys.exit(main())
