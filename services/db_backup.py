import asyncio
import json
from datetime import datetime, timezone

from loguru import logger

from config import settings
from database.session import _client as db_client, get_config, _validate_identifier
from storage.r2 import _r2 as r2_storage, configure_r2_dynamic
from services.backup_schema import (
    BACKUP_SCHEMA, get_backup_tables, get_conflict_col,
)

# ─── 表清单(单一事实源: services/backup_schema.py) ───
# 保留向后兼容的别名,等价于原 SMALL_TABLES / _LARGE_TABLES / _TABLE_WHERE
# 新增表时只需在 backup_schema.BACKUP_SCHEMA 中添加条目,无需修改本文件
SMALL_TABLES = set(get_backup_tables())
_LARGE_TABLES = {t.name for t in BACKUP_SCHEMA.values() if t.is_large}
BACKUP_TABLES = SMALL_TABLES

# 每个表可选的 WHERE 条件,用于过滤备份范围(从 BACKUP_SCHEMA 派生)
_TABLE_WHERE = {t.name: t.where_clause for t in BACKUP_SCHEMA.values() if t.where_clause}

# 备份保留份数（超出则自动清理最旧的）
MAX_BACKUP_RETENTION = 168  # 7天 × 24小时 / 1小时间隔 ≈ 168 份

# P0-3: 备份中不再脱敏敏感字段 (api_hash / r2_secret_key / r2_access_key)。
# 备份仅存储在运维自有 R2 桶内(需可信环境),若替换为 ***REDACTED*** 占位符,
# 会导致 db_restore 恢复后中继账号与 R2 凭证变为占位符、不可用(废库)。
# 因此保留真实值,确保恢复后凭证可用。_SENSITIVE_FIELDS 置空即关闭脱敏。
_SENSITIVE_FIELDS: set[str] = set()
_REDACTED_VALUE = "***REDACTED***"  # 保留常量以便未来按需启用,当前脱敏集为空故不会被写入


def _redact_secrets(data: dict) -> dict:
    """脱敏备份数据中的敏感字段，不影响原始数据库。"""
    tables = data.get("tables", {})
    for table_name, rows in tables.items():
        if table_name in ("backup_config", "kv_config"):
            for row in rows:
                # N-15-1: 按 config_key 匹配行级密钥（如 config_key="r2_secret_key" → config_value 脱敏）
                config_key = (row.get("config_key") or "").lower()
                if config_key in _SENSITIVE_FIELDS:
                    row["config_value"] = _REDACTED_VALUE
        if table_name == "relay_accounts":
            for row in rows:
                for key in list(row.keys()):
                    if key.lower() in _SENSITIVE_FIELDS:
                        row[key] = _REDACTED_VALUE
    return data


async def backup_all_tables() -> dict:
    """备份核心元数据表（含 codes/file_records 的取件映射）。

    大表（decode_logs/jobs/pending_uploads/rotate_log）跳过：
    - decode_logs/jobs 是短期流水数据，无需长期备份
    - pending_uploads 是瞬时状态，重启后从频道重放
    - rotate_log 是审计日志，数据量大但非核心
    """
    results = {}
    for table in sorted(BACKUP_TABLES):
        try:
            safe_name = table.replace('"', '""')
            where = _TABLE_WHERE.get(table)
            # 所有核心表均不限制行数，确保完整备份可恢复
            if where:
                sql = f'SELECT * FROM "{safe_name}" WHERE {where}'
            else:
                sql = f'SELECT * FROM "{safe_name}"'
            # 使用公共 fetch API,避免访问 _pool 私有属性
            records = await db_client.fetch(sql)
            results[table] = [dict(r) for r in records]
            logger.debug(f"[Backup] {table}: {len(records)} 行")
        except Exception as e:
            # 表不存在时降级为 DEBUG(如 kv_config 在部分部署中不存在)
            if "does not exist" in str(e):
                logger.debug(f"[Backup] 表 {table} 不存在,跳过: {e}")
            else:
                logger.warning(f"[Backup] 跳过表 {table}: {e}")

    return {"backup_time": datetime.now(timezone.utc).isoformat(), "tables": results}


async def run_db_backup():
    # 确保数据库连接池已初始化（某些场景下 _auto_seed 可能未成功初始化）
    if not db_client.is_connected:
        try:
            from database.session import init_db
            await init_db()
        except Exception as e:
            logger.warning(f"数据库连接初始化失败,跳过备份: {e}")
            return

    try:
        await _run_backup_loop()
    finally:
        # 确保退出时关闭数据库连接和 SQLite 缓存,避免进程挂起被 SIGKILL
        try:
            from database.session import close_db
            await close_db()
        except Exception:
            pass
        try:
            await r2_storage.close()
        except Exception:
            pass
        logger.info("[db_backup] 资源已清理,进程退出")


async def _run_backup_loop():
    """备份主循环(由 run_db_backup 调用,确保 finally 中清理资源)。"""
    enabled_cfg = await get_config("db_backup_enabled")
    if enabled_cfg is None:
        enabled = settings.DB_BACKUP_ENABLED
    else:
        enabled = enabled_cfg.lower() == "true"
    if not enabled:
        logger.info("数据库备份未启用(DB_BACKUP_ENABLED=false),跳过启动")
        return

    # R26-M1: R2 凭证优先从 config 表读取（r2_secret_key 解密），fallback .env
    await configure_r2_dynamic()
    if not r2_storage._access_key or not r2_storage._secret_key:
        logger.warning("R2 凭证未配置(.env 和 config 表均无),数据库备份跳过")
        return

    interval_cfg = await get_config("db_backup_interval")
    if interval_cfg is None:
        interval = settings.DB_BACKUP_INTERVAL_MINUTES
    else:
        try:
            interval = max(int(interval_cfg), 1)
        except (ValueError, TypeError):
            logger.warning(f"[db_backup] db_backup_interval 配置值 '{interval_cfg}' 无效,使用默认值 {settings.DB_BACKUP_INTERVAL_MINUTES}")
            interval = settings.DB_BACKUP_INTERVAL_MINUTES
    logger.info("CockroachDB 数据库备份服务启动,间隔 {} 分钟", interval)

    while True:
        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            key = f"db_backup/db_backup_{timestamp}.json"
            data = await backup_all_tables()
            # N-M9: 脱敏备份数据中的敏感字段
            data = _redact_secrets(data)
            content = json.dumps(data, default=str, ensure_ascii=False).encode("utf-8")
            await r2_storage.upload(key, content, "application/json")
            total_rows = sum(len(v) for v in data["tables"].values())
            logger.info(
                f"数据库已备份到 R2: {key} ({len(content)} 字节, "
                f"{len(data['tables'])} 表, {total_rows} 行)"
            )

            for table in data["tables"]:
                t_content = json.dumps(
                    data["tables"][table], default=str, ensure_ascii=False
                ).encode("utf-8")
                await r2_storage.upload(
                    f"db_backup/latest_{table}.json",
                    t_content,
                    "application/json",
                )

            # 清理旧备份，仅保留最近 MAX_BACKUP_RETENTION 份
            try:
                await _cleanup_old_backups(r2_storage)
            except Exception as cleanup_err:
                logger.warning(f"[db_backup] 清理旧备份失败(不影响本次备份): {cleanup_err}")

        except (SystemExit, KeyboardInterrupt):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"数据库备份失败: {e}")

        # 配置重读也需异常保护:CRDB 临时不可用时 get_config 会抛异常
        # 若不保护,整个 run_db_backup 协程会崩溃,备份服务永久停止(需手动重启)
        try:
            interval_cfg = await get_config("db_backup_interval")
            if interval_cfg is None:
                interval = settings.DB_BACKUP_INTERVAL_MINUTES
            else:
                try:
                    interval = max(int(interval_cfg), 1)
                except (ValueError, TypeError):
                    interval = settings.DB_BACKUP_INTERVAL_MINUTES
        except Exception as cfg_err:
            logger.warning(f"[db_backup] 读取备份间隔失败,使用默认值: {cfg_err}")
            interval = settings.DB_BACKUP_INTERVAL_MINUTES
        await asyncio.sleep(interval * 60)


async def _cleanup_old_backups(storage, prefix: str = "db_backup/db_backup_"):
    """清理旧备份文件，仅保留最近 MAX_BACKUP_RETENTION 份。

    按 key 名字典序（即时间序）排序，删除超出保留数量的最旧备份。
    不影响 latest_{table}.json 文件（前缀不同）。
    """
    objects = await storage.list_objects(prefix=prefix, max_keys=1000)
    if len(objects) <= MAX_BACKUP_RETENTION:
        return
    # 按 key 排序（key 含时间戳，字典序等价于时间序）
    objects.sort(key=lambda obj: obj.get("key", ""))
    to_delete = objects[:len(objects) - MAX_BACKUP_RETENTION]
    for obj in to_delete:
        key = obj.get("key", "")
        if not key:
            continue
        try:
            await storage.delete(key)
            logger.debug(f"[db_backup] 已清理旧备份: {key}")
        except Exception as e:
            logger.warning(f"[db_backup] 删除旧备份失败 {key}: {e}")
    if to_delete:
        logger.info(f"[db_backup] 清理了 {len(to_delete)} 份旧备份，保留 {MAX_BACKUP_RETENTION} 份")


async def list_backups() -> list[dict]:
    """列出 R2 中的所有备份文件，按时间倒序返回。

    供管理后台/admin_bot 调用，展示可恢复的备份列表。
    """
    # R26-M1: R2 凭证优先从 config 表读取（r2_secret_key 解密），fallback .env
    await configure_r2_dynamic()
    if not r2_storage._access_key:
        return []
    objects = await r2_storage.list_objects(prefix="db_backup/db_backup_", max_keys=1000)
    # 按时间倒序（key 含时间戳，倒序 = 最新在前）
    objects.sort(key=lambda obj: obj.get("key", ""), reverse=True)
    return objects


async def restore_from_backup(key: str, tables: list[str] | None = None, merge: bool = False) -> dict:
    """从 R2 备份恢复数据库。

    Args:
        key: R2 对象 key（如 db_backup/db_backup_20240101_120000.json）
        tables: 仅恢复指定表；None 则恢复备份中的所有表
        merge: True=增量补充(冲突保留现有数据); False=覆盖(清空后写入,默认)

    Returns:
        {"restored": {table: rows}, "skipped": [tables], "errors": [msgs]}
    """
    # R26-M1: R2 凭证优先从 config 表读取（r2_secret_key 解密），fallback .env
    await configure_r2_dynamic()
    if not r2_storage._access_key:
        raise RuntimeError("R2 凭证未配置(.env 和 config 表均无)，无法恢复")

    # 下载备份
    content = await r2_storage.download(key)
    data = json.loads(content)
    backup_tables = data.get("tables", {})

    if tables:
        restore_tables = {t: backup_tables[t] for t in tables if t in backup_tables}
        skipped = [t for t in tables if t not in backup_tables]
    else:
        restore_tables = backup_tables
        skipped = []

    result = {"restored": {}, "skipped": skipped, "errors": []}

    if not db_client.is_connected:
        from database.session import init_db
        await init_db()

    # 冲突策略从 BACKUP_SCHEMA 派生(单一事实源: services/backup_schema.py)
    # - 复合主键(len(pk_columns) > 1) → ON CONFLICT (col1, col2, ...) DO NOTHING
    # - 单列冲突(conflict_col 非空) → ON CONFLICT ("col") DO NOTHING
    # - 自增主键(conflict_col 为空) → 退化为普通 INSERT(追加式日志语义可接受)

    for table_name, rows in restore_tables.items():
        # P0-2: 表名格式白名单校验(防 SQL 注入),非法表名跳过并记录告警
        try:
            safe_name = _validate_identifier(table_name)
        except ValueError as e:
            logger.warning(f"[db_restore] 跳过非法表名: {e}")
            result["skipped"].append(table_name)
            continue
        if not rows:
            result["restored"][table_name] = 0
            continue
        try:
            # 使用事务保证原子性
            await db_client.execute("BEGIN")
            try:
                if not merge:
                    # 覆盖模式:清空目标表（恢复前清空，避免主键冲突）
                    await db_client.execute(f'TRUNCATE TABLE "{safe_name}" RESTART IDENTITY CASCADE')

                # 确定 ON CONFLICT 子句(仅 merge 模式,从 BACKUP_SCHEMA 派生)
                conflict_clause = ""
                if merge:
                    _schema = BACKUP_SCHEMA.get(table_name)
                    if _schema and len(_schema.pk_columns) > 1:
                        # 复合主键: ON CONFLICT (col1, col2, ...) DO NOTHING
                        # 覆盖 message_backups / manifest 等复合主键表
                        _pk_cols = ", ".join(f'"{c}"' for c in _schema.pk_columns)
                        conflict_clause = f' ON CONFLICT ({_pk_cols}) DO NOTHING'
                    elif _schema and _schema.conflict_col:
                        # 单列冲突: ON CONFLICT ("col") DO NOTHING
                        conflict_clause = f' ON CONFLICT ("{_schema.conflict_col}") DO NOTHING'
                    else:
                        # 自增主键或未知冲突列,merge 模式下退化为普通 INSERT
                        # 遇到冲突会报错并由事务 ROLLBACK
                        logger.warning(f"[db_restore] 表 {safe_name} 未知冲突列,merge 模式可能因主键冲突失败")

                # 批量插入
                inserted = 0
                for row in rows:
                    # P0-2: 每个列名格式校验,非法列名跳过该行并记录告警(绝不拼接未校验标识符)
                    try:
                        cols = [_validate_identifier(c) for c in row.keys()]
                    except ValueError as e:
                        logger.warning(f"[db_restore] 表 {safe_name} 含非法列名,跳过该行: {e}")
                        continue
                    placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
                    col_list = ", ".join(f'"{c}"' for c in cols)
                    params = [row[c] for c in cols]
                    sql = f'INSERT INTO "{safe_name}" ({col_list}) VALUES ({placeholders}){conflict_clause}'
                    await db_client.execute(sql, params)
                    inserted += 1
                await db_client.execute("COMMIT")
                result["restored"][table_name] = inserted
                mode_text = "增量补充" if merge else "覆盖恢复"
                logger.info(f"[db_restore] {mode_text} 表 {table_name}: {inserted} 行")
            except Exception as inner_e:
                # ROLLBACK 失败不应吞噬原始异常,使用嵌套 try/except 保护
                try:
                    await db_client.execute("ROLLBACK")
                except Exception as rollback_err:
                    logger.error(f"[db_restore] ROLLBACK 失败 (table={table_name}): {rollback_err}")
                raise inner_e
        except Exception as e:
            result["errors"].append(f"{table_name}: {e}")
            logger.error(f"[db_restore] 恢复表 {table_name} 失败: {e}")

    logger.info(
        f"[db_restore] 恢复完成: {sum(result['restored'].values())} 行, "
        f"{len(result['errors'])} 个错误, 模式={'merge' if merge else 'overwrite'}"
    )
    return result
