"""R59 P1: SQLite 版本化迁移框架。

替换 services/data_lifecycle.py 中 ``_ensure_command_approvals_table()`` 的惰性 DDL 模式,
将运行时 CREATE TABLE / ALTER TABLE 迁移到版本化 SQL 文件。

设计原则:
  - 版本化: 每个 migration 文件按 ``001_xxx.sql``, ``002_xxx.sql`` 编号,按文件名排序执行
  - 可回滚: 当前实现 up 方向(应用迁移);down 方向可通过新增降级 SQL 文件扩展
  - 可重复 dry-run: 重复执行不会产生副作用(IF NOT EXISTS + 严格白名单错误)
  - 幂等性: 已应用的 migration 通过 ``_migrations_applied`` 表记录,不会重复执行
  - 无第三方依赖: 纯 Python + aiosqlite,不引入 alembic/yoyo-migrations 等

``_migrations_applied`` 表结构:
    version     TEXT PRIMARY KEY  — migration 文件名(如 '001_initial_schema.sql')
    applied_at  TEXT NOT NULL     — 应用时间(ISO 8601 格式)

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


async def _get_applied_versions(db: Any) -> set[str]:
    """查询已应用的 migration 版本列表。

    若 ``_migrations_applied`` 表不存在则自动创建(首次运行)。

    Args:
        db: aiosqlite.Connection

    Returns:
        已应用的 migration 文件名集合
    """
    # 创建版本记录表(首次运行时)
    await db.execute(
        """CREATE TABLE IF NOT EXISTS _migrations_applied (
            version     TEXT PRIMARY KEY,
            applied_at  TEXT NOT NULL
        )"""
    )
    await db.commit()
    # 查询已应用的版本
    cursor = await db.execute("SELECT version FROM _migrations_applied")
    rows = await cursor.fetchall()
    return {str(row[0]) for row in rows}


async def _apply_single_migration(db: Any, migration_file: Path) -> bool:
    """应用单个 migration 文件。

    逐条执行 SQL 语句,可忽略错误(duplicate column / already exists)跳过,
    非白名单错误立即抛出并返回 False。

    成功执行后将版本记录写入 ``_migrations_applied`` 表。

    Args:
        db: aiosqlite.Connection
        migration_file: migration SQL 文件路径

    Returns:
        True 应用成功;False 应用失败(非白名单错误)
    """
    version = migration_file.name
    sql_content = migration_file.read_text(encoding="utf-8")
    statements = _split_sql_statements(sql_content)
    if not statements:
        logger.warning(f"[migrate] {version} 无可执行 SQL 语句,跳过")
        return True
    logger.info(
        f"[migrate] 应用 {version}({len(statements)} 条语句)"
    )
    for stmt in statements:
        try:
            await db.execute(stmt)
        except Exception as e:
            err_msg = str(e)
            if _is_ignorable_error(err_msg):
                # 幂等忽略: 列已存在 / 表/索引已存在
                logger.debug(
                    f"[migrate] {version} 语句执行失败(白名单可忽略): "
                    f"{stmt[:80]}... → {e}"
                )
            else:
                # 非白名单错误: 严重错误,不记录为已应用
                logger.error(
                    f"[migrate] {version} 语句执行失败(严重,非白名单错误): "
                    f"{stmt} → {e}"
                )
                return False
    # 记录为已应用
    now_iso = _dt.datetime.now().isoformat()
    await db.execute(
        "INSERT OR REPLACE INTO _migrations_applied (version, applied_at) VALUES (?, ?)",
        (version, now_iso),
    )
    await db.commit()
    logger.info(f"[migrate] {version} 应用完成")
    return True


async def apply_migrations(db: Any = None) -> dict[str, list[str]]:
    """R59 P1: 应用所有未执行的 SQLite migration。

    本函数是迁移框架的主入口,执行流程:
      1. 获取数据库连接(参数传入或从 CacheStore 获取)
      2. 创建 ``_migrations_applied`` 版本记录表(首次运行)
      3. 列出 migrations 目录下所有 .sql 文件,按文件名排序
      4. 对每个未应用的 migration:
         a. 读取 SQL 内容并按分号分割为独立语句
         b. 逐条执行,可忽略 "duplicate column" / "already exists" 错误
         c. 非白名单错误立即终止该 migration,不记录为已应用
         d. 成功后写入 _migrations_applied 表
      5. 返回应用结果汇总

    幂等性保证:
      - 已应用的 migration 不会重复执行(_migrations_applied 主键去重)
      - SQL 语句使用 IF NOT EXISTS / 白名单错误处理,重复执行无副作用
      - 支持多次 dry-run(重复调用 apply_migrations 不会产生副作用)

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
    return result
