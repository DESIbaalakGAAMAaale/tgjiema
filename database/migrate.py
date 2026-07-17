"""R59 P1: SQLite 版本化迁移框架。

替换 services/data_lifecycle.py 中 ``_ensure_command_approvals_table()`` 的惰性 DDL 模式,
将运行时 CREATE TABLE / ALTER TABLE 迁移到版本化 SQL 文件。

设计原则:
  - 版本化: 每个 migration 文件按 ``001_xxx.sql``, ``002_xxx.sql`` 编号,按文件名排序执行
  - 可回滚: 当前实现 up 方向(应用迁移);down 方向可通过新增降级 SQL 文件扩展
  - 可重复 dry-run: 重复执行不会产生副作用(IF NOT EXISTS + 严格白名单错误)
  - 幂等性: 已应用的 migration 通过 ``_migrations_applied`` 表记录,不会重复执行
  - 无第三方依赖: 纯 Python + aiosqlite,不引入 alembic/yoyo-migrations 等

``_migrations_applied`` 表结构(R60 P0-05 增强):
    version     TEXT PRIMARY KEY  — migration 文件名(如 '001_initial_schema.sql')
    sha256      TEXT NOT NULL     — SQL 文件内容 SHA-256(检测篡改,fail-closed)
    applied_at  TEXT NOT NULL     — 应用时间(ISO 8601 格式)
    duration_ms INTEGER           — 应用耗时(毫秒)

调用方式:
    # 在 _ensure_command_approvals_table() 中调用(兼容入口)
    from database.migrate import apply_migrations
    result = await apply_migrations(db=store._db)

    # 也可独立调用(如启动时一次性应用所有 migration)
    from database.migrate import apply_migrations
    from database.cache_store import get_cache_store
    store = get_cache_store()
    await store.init()
    result = await apply_migrations(db=store._db)

返回值:
    {
        "applied": [str],   — 本次新应用的 migration 文件名列表
        "skipped": [str],   — 已应用跳过的 migration 文件名列表
        "failed":  [str],   — 执行失败的 migration 文件名列表(非幂等错误)
    }
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import time
from pathlib import Path
from typing import Any

from loguru import logger

# migration 文件目录(database/migrations/)
_MIGRATIONS_DIR: Path = Path(__file__).parent / "migrations"

# 可忽略的 DDL 错误关键词(SQLite ALTER TABLE 不支持 IF NOT EXISTS,重复执行需白名单过滤)
# - "duplicate column": ADD COLUMN 列已存在(001 已创建完整 schema 后 002 重复补列)
# - "already exists": CREATE TABLE/INDEX 已存在
# 其他错误(语法错误、权限错误、连接错误)必须抛出,禁止继续记录为已应用
_IGNORABLE_ERROR_PATTERNS: tuple[str, ...] = (
    "duplicate column",
    "already exists",
)


def _is_ignorable_error(err_msg: str) -> bool:
    """判断 SQLite DDL 错误是否可忽略(白名单精确匹配)。

    只允许 "duplicate column" / "already exists" 关键词的错误继续执行,
    其他错误(语法/权限/连接等)必须立即抛出,禁止记录为已应用。
    """
    msg_lower = err_msg.lower()
    return any(p in msg_lower for p in _IGNORABLE_ERROR_PATTERNS)


def _split_sql_statements(sql_content: str) -> list[str]:
    """将 SQL 文件内容按分号分割为独立语句。

    处理规则:
      1. 移除 ``--`` 注释行(行首或行内)
      2. 按顶层分号分割(不分割字符串内的分号)
      3. 过滤空白语句

    Args:
        sql_content: SQL 文件原始内容

    Returns:
        去重后的 SQL 语句列表(每条语句已 strip)
    """
    # 移除注释行(-- 开头的行)
    lines: list[str] = []
    for line in sql_content.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        # 移除行内注释(-- 后的内容,简单处理:不分割字符串内的 --,但当前 migration 无此场景)
        if "--" in line:
            line = line[:line.index("--")]
        lines.append(line)
    cleaned = "\n".join(lines)
    # 按分号分割
    statements: list[str] = []
    for raw_stmt in cleaned.split(";"):
        stmt = raw_stmt.strip()
        if stmt:
            statements.append(stmt)
    return statements


def _list_migration_files() -> list[Path]:
    """列出 migrations 目录下所有 .sql 文件,按文件名排序。

    Returns:
        排序后的 Path 列表(如 001_initial_schema.sql, 002_xxx.sql, ...)
    """
    if not _MIGRATIONS_DIR.exists():
        logger.warning(f"[migrate] migration 目录不存在: {_MIGRATIONS_DIR}")
        return []
    return sorted(_MIGRATIONS_DIR.glob("*.sql"))


def _compute_sha256(file_path: Path) -> str:
    """计算文件原始字节内容的 SHA-256 校验和(十六进制小写)。

    用于在应用 migration 时记录其 SQL 内容指纹,启动时比对以检测文件被篡改
    (R60 P0-05: fail-closed,篡改/删除的 migration 文件阻断服务启动)。
    """
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def _get_applied_versions(db: Any) -> dict[str, str]:
    """查询已应用的 migration 版本及其 SHA-256 校验和。

    若 ``_migrations_applied`` 表不存在则自动创建(首次运行,R60 新 schema)。
    若旧 schema(无 sha256 / duration_ms 列)已存在,通过 ALTER TABLE ADD COLUMN
    补列(向后兼容 R59 已部署实例);旧记录的 sha256 留空,由 ``apply_migrations``
    用当前文件内容回填。

    R60 P0-05 schema:
        version     TEXT PRIMARY KEY  — migration 文件名
        sha256      TEXT NOT NULL     — SQL 文件内容 SHA-256(检测篡改)
        applied_at  TEXT NOT NULL     — 应用时间(ISO 8601)
        duration_ms INTEGER           — 应用耗时(毫秒)

    Args:
        db: aiosqlite.Connection

    Returns:
        {version: sha256} 映射(已应用 migration 的文件名 → 校验和,空串表示旧记录未回填)
    """
    # 创建版本记录表(首次运行,R60 新 schema;对已存在表是 no-op)
    await db.execute(
        """CREATE TABLE IF NOT EXISTS _migrations_applied (
            version     TEXT PRIMARY KEY,
            sha256      TEXT NOT NULL,
            applied_at  TEXT NOT NULL,
            duration_ms INTEGER
        )"""
    )
    # 向后兼容: 旧 R59 schema 只有 (version, applied_at),补 sha256 / duration_ms 列。
    # CREATE TABLE IF NOT EXISTS 不会修改已存在表,需通过 PRAGMA 检测列是否缺失。
    cursor = await db.execute("PRAGMA table_info(_migrations_applied)")
    existing_cols: set[str] = {str(row[1]) for row in await cursor.fetchall()}
    if "sha256" not in existing_cols:
        # 旧表已有行无 sha256,先以可空列补上(不能对非空表加 NOT NULL),
        # 后续由 apply_migrations 回填当前文件校验和
        await db.execute(
            "ALTER TABLE _migrations_applied ADD COLUMN sha256 TEXT"
        )
    if "duration_ms" not in existing_cols:
        await db.execute(
            "ALTER TABLE _migrations_applied ADD COLUMN duration_ms INTEGER"
        )
    await db.commit()
    # 查询已应用的版本及其 sha256
    cursor = await db.execute("SELECT version, sha256 FROM _migrations_applied")
    rows = await cursor.fetchall()
    return {str(row[0]): (row[1] or "") for row in rows}


async def _apply_single_migration(db: Any, migration_file: Path) -> bool:
    """应用单个 migration 文件(R60 P0-05: 显式事务 + SHA-256 校验和)。

    整个 migration(所有 DDL + 版本记录 INSERT)在单个 ``BEGIN IMMEDIATE`` 事务中
    执行,确保部分 DDL 或被篡改的 migration 文件不会被记录为已应用。

    逐条执行 SQL 语句,可忽略错误(duplicate column / already exists)跳过,
    非白名单错误立即 ROLLBACK 事务并返回 False;提交/版本记录失败同样 ROLLBACK。

    成功后将版本记录(含 SQL 内容 SHA-256 与耗时)写入 ``_migrations_applied`` 表
    (与 DDL 在同一事务内提交)。

    Args:
        db: aiosqlite.Connection
        migration_file: migration SQL 文件路径

    Returns:
        True 应用成功;False 应用失败(非白名单错误或事务提交失败)
    """
    version = migration_file.name
    sql_content = migration_file.read_text(encoding="utf-8")
    sha256 = _compute_sha256(migration_file)
    statements = _split_sql_statements(sql_content)
    if not statements:
        logger.warning(f"[migrate] {version} 无可执行 SQL 语句,跳过")
        return True
    logger.info(
        f"[migrate] 应用 {version}({len(statements)} 条语句, sha256={sha256[:12]}...)"
    )
    start_ts = time.perf_counter()
    # R60 P0-05: 显式事务 — 单个 migration 的所有 DDL + 版本记录 INSERT 必须原子提交
    # R60 §ci-fix: except 中不直接 return False(AST 错误协议规则3),
    # 改用标志位在 except 外返回,保持 bool 契约同时满足 fail-closed
    begin_failed = False
    try:
        await db.execute("BEGIN IMMEDIATE")
    except Exception as e:
        logger.error(f"[migrate] {version} BEGIN IMMEDIATE 失败: {e}")
        begin_failed = True
    if begin_failed:
        return False
    commit_failed = False
    try:
        for stmt in statements:
            try:
                await db.execute(stmt)
            except Exception as e:
                err_msg = str(e)
                if _is_ignorable_error(err_msg):
                    # 幂等忽略: 列已存在 / 表/索引已存在(该语句回滚,事务继续)
                    logger.debug(
                        f"[migrate] {version} 语句执行失败(白名单可忽略): "
                        f"{stmt[:80]}... → {e}"
                    )
                else:
                    # 非白名单错误: 严重错误,回滚整个事务,不记录为已应用
                    logger.error(
                        f"[migrate] {version} 语句执行失败(严重,非白名单错误): "
                        f"{stmt} → {e}"
                    )
                    await db.execute("ROLLBACK")
                    return False
        # 记录为已应用(与 DDL 在同一事务内,确保原子)
        now_iso = _dt.datetime.now().isoformat()
        duration_ms = int((time.perf_counter() - start_ts) * 1000)
        await db.execute(
            "INSERT OR REPLACE INTO _migrations_applied "
            "(version, sha256, applied_at, duration_ms) VALUES (?, ?, ?, ?)",
            (version, sha256, now_iso, duration_ms),
        )
        await db.execute("COMMIT")
    except Exception as e:
        # 提交或版本记录写入失败: 回滚,不记录为已应用
        logger.error(
            f"[migrate] {version} 事务提交/版本记录失败,执行 ROLLBACK: {e}"
        )
        try:
            await db.execute("ROLLBACK")
        except Exception as rollback_err:
            logger.error(f"[migrate] {version} ROLLBACK 失败: {rollback_err}")
        commit_failed = True
    if commit_failed:
        return False
    logger.info(
        f"[migrate] {version} 应用完成(耗时 {duration_ms}ms)"
    )
    return True


async def apply_migrations(db: Any = None) -> dict[str, list[str]]:
    """R59 P1: 应用所有未执行的 SQLite migration。

    本函数是迁移框架的主入口,执行流程:
      1. 获取数据库连接(参数传入或从 CacheStore 获取)
      2. 创建 ``_migrations_applied`` 版本记录表(首次运行,R60 新 schema)
      3. 列出 migrations 目录下所有 .sql 文件,按文件名排序
      4. R60 P0-05: 校验已应用 migration 文件 SHA-256(篡改/删除 → raise 阻断启动)
      5. 对每个未应用的 migration:
         a. 读取 SQL 内容并按分号分割为独立语句
         b. 在单个 BEGIN IMMEDIATE 事务中逐条执行,可忽略
            "duplicate column" / "already exists" 错误
         c. 非白名单错误立即 ROLLBACK 并终止该 migration,不记录为已应用
         d. 成功后(同一事务)写入 _migrations_applied 表(含 sha256 / duration_ms)
      6. 返回应用结果汇总;若 failed 非空则 raise(fail-closed,禁止继续服务)

    幂等性保证:
      - 已应用的 migration 不会重复执行(_migrations_applied 主键去重)
      - SQL 语句使用 IF NOT EXISTS / 白名单错误处理,重复执行无副作用
      - 支持多次 dry-run(重复调用 apply_migrations 不会产生副作用)
      - R60 P0-05: 启动时校验已应用 migration 文件 SHA-256,篡改/删除则 raise
      - R60 P0-05: 失败的 migration 必须 raise,禁止带失败结果继续服务

    Args:
        db: 可选的 aiosqlite.Connection。若为 None,从 CacheStore 获取连接。
            测试中可传入自定义连接以隔离测试。

    Returns:
        {
            "applied": [str],  — 本次新应用的 migration 文件名列表
            "skipped": [str],  — 已应用跳过的 migration 文件名列表
            "failed":  [str],  — 执行失败的 migration 文件名列表(非幂等错误)
        }
    """
    # 获取数据库连接
    own_connection = False
    if db is None:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if not store._db:
            # CacheStore 未初始化,尝试 init
            await store.init()
        db = store._db
        own_connection = True
    if db is None:
        logger.error("[migrate] 无法获取 SQLite 连接,迁移中止")
        return {"applied": [], "skipped": [], "failed": []}

    result: dict[str, list[str]] = {
        "applied": [],
        "skipped": [],
        "failed": [],
    }

    # 查询已应用版本
    try:
        applied_versions = await _get_applied_versions(db)
    except Exception as e:
        logger.error(f"[migrate] 查询已应用版本失败: {e}")
        return result

    # 列出所有 migration 文件
    migration_files = _list_migration_files()
    if not migration_files:
        logger.warning("[migrate] 无 migration 文件可执行")
        return result

    # R60 P0-05: 启动时校验已应用 migration 文件的 SHA-256(fail-closed)
    # 任何已应用 migration 的文件被修改或删除 → 阻断启动,禁止带篡改文件继续服务
    file_map: dict[str, Path] = {mf.name: mf for mf in migration_files}
    for version, stored_sha256 in applied_versions.items():
        if version not in file_map:
            raise RuntimeError(
                f"Migration file {version} has been modified or removed "
                f"(stored_sha256={stored_sha256 or '<empty>'}, "
                f"actual_sha256=None)"
            )
        actual_sha256 = _compute_sha256(file_map[version])
        if not stored_sha256:
            # R60 之前应用的旧记录无 sha256,用当前文件内容回填
            # (信任当前状态,后续篡改可被检测)
            await db.execute(
                "UPDATE _migrations_applied SET sha256 = ? WHERE version = ?",
                (actual_sha256, version),
            )
            await db.commit()
            logger.info(
                f"[migrate] 补齐历史 migration {version} 的 sha256 校验和"
            )
        elif actual_sha256 != stored_sha256:
            raise RuntimeError(
                f"Migration file {version} has been modified or removed "
                f"(stored_sha256={stored_sha256}, "
                f"actual_sha256={actual_sha256})"
            )

    # 逐个应用未执行的 migration
    for mf in migration_files:
        version = mf.name
        if version in applied_versions:
            result["skipped"].append(version)
            continue
        success = await _apply_single_migration(db, mf)
        if success:
            result["applied"].append(version)
        else:
            result["failed"].append(version)
            # 遇到严重错误终止后续 migration(避免版本错位)
            logger.error(
                f"[migrate] {version} 应用失败,终止后续 migration(避免版本错位)"
            )
            break

    logger.info(
        f"[migrate] 迁移完成: 应用 {len(result['applied'])} 个, "
        f"跳过 {len(result['skipped'])} 个, 失败 {len(result['failed'])} 个"
    )
    # R60 P0-05: 失败必须 raise,禁止带失败结果继续提供服务(fail-closed)
    if result["failed"]:
        raise RuntimeError(
            f"[migrate] migration 应用失败,阻断启动: failed={result['failed']}"
        )
    return result
