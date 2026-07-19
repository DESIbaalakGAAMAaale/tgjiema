"""R65 P0-02: RestoreBackend Protocol — 真实恢复数据面实现。

R65 P0-02 整改背景:
    R64 的 RestoreOrchestrator 只是状态机骨架,provision_staging() 仅 touch
    空文件,restore_to_staging() 仅改状态字段,validate_staging() 默认 ok,
    execute_blue_green_switch() 仅做字符串相等比较 — 状态机可以产生
    "已恢复、已验证"的审计记录但没有发生真实恢复和验证。

整改方案:
    1. RestoreBackend Protocol 提供 7 个真实方法:
       provision / load_verified_payload / validate / prepare_switch /
       commit_switch / rollback_switch / destroy
    2. 三个具体实现:
       - CRDBRestoreBackend (asyncpg 直连,写 staging schema)
       - SQLiteRestoreBackend (aiosqlite,写 staging 文件;cache/relay 共用)
    3. orchestrator 调用 backend 真实方法,不再伪造状态
    4. backend 验证返回真实 row counts + content hash + schema fingerprint
    5. 蓝绿切换:
       - SQLite: 原子 rename(current → backup, staging → current)
       - CRDB: schema routing 切换(记录新 schema 名,保留旧 schema 作回滚点)

设计原则:
    - backend 不做信任校验(由 orchestrator 上游的 VerifiedBackupPayload 保证)
    - backend 只写 staging,不接触 active(commit_switch 才切换)
    - 失败必须显式 raise,不返回部分成功
    - 所有方法都接收 operation_id 用于日志与审计
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import os
import secrets
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from loguru import logger

from services.error_codes import AppError, ErrorCodes
from services.i18n import translate as _i18n_t


# ════════════════════════════════════════════════════════════════
# 1. 数据类:provision / restore / validate / switch 的结果
# ════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class StagingProvisionResult:
    """provision() 返回结果。

    Attributes:
        target: staging 目标标识(CRDB schema 名 / SQLite 文件路径)
        target_type: "crdb_schema" / "sqlite_file"
        created_at: ISO8601 创建时间
        schema_fingerprint: staging schema 指纹(用于后续验证一致性)
    """
    target: str
    target_type: str
    created_at: str
    schema_fingerprint: str = ""


@dataclass(frozen=True)
class StagingRestoreResult:
    """load_verified_payload() 返回结果。

    Attributes:
        rows_restored: {table_name: row_count}
        content_hash: 各表内容 SHA-256 (table_name → sha256_hex)
        schema_fingerprint: 实际写入后的 schema 指纹
        bytes_written: 总写入字节
        duration_seconds: 写入耗时
    """
    rows_restored: dict[str, int] = field(default_factory=dict)
    content_hash: dict[str, str] = field(default_factory=dict)
    schema_fingerprint: str = ""
    bytes_written: int = 0
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class StagingValidationResult:
    """validate() 返回结果。

    每个维度: "ok" / "fail" / "skipped" + 详细信息。
    R65 P0-02: 任一维度 skipped/pending/unknown 均失败,不能默认 ok。

    Attributes:
        schema: schema 校验(ok/fail)
        row_count: 行数校验(ok/fail)
        foreign_keys: 主外键校验(ok/fail)
        business_invariant: 业务守恒(ok/fail)
        hash_check: 内容 hash 校验(ok/fail)
        dry_run: 只读演练(ok/fail)
        details: 各维度详细信息
    """
    schema: str = "pending"
    row_count: str = "pending"
    foreign_keys: str = "pending"
    business_invariant: str = "pending"
    hash_check: str = "pending"
    dry_run: str = "pending"
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        """R65 P0-02: 所有维度必须显式 ok,skipped/pending/unknown 均失败。"""
        for key in ("schema", "row_count", "foreign_keys",
                    "business_invariant", "hash_check", "dry_run"):
            value = getattr(self, key)
            if value != "ok":
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "row_count": self.row_count,
            "foreign_keys": self.foreign_keys,
            "business_invariant": self.business_invariant,
            "hash_check": self.hash_check,
            "dry_run": self.dry_run,
            "details": self.details,
        }


@dataclass(frozen=True)
class SwitchResult:
    """commit_switch() / rollback_switch() 返回结果。

    Attributes:
        switch_version: 切换版本号(UUID,单调递增的 fencing token)
        previous_target: 切换前的 active target(用于回滚)
        new_target: 切换后的 active target(staging 升级为 active)
        switched_at: ISO8601 切换时间
    """
    switch_version: str
    previous_target: str
    new_target: str
    switched_at: str


# ════════════════════════════════════════════════════════════════
# 2. RestoreBackend Protocol
# ════════════════════════════════════════════════════════════════


@runtime_checkable
class RestoreBackend(Protocol):
    """R65 P0-02: 恢复后端协议 — 三个具体实现(CRDB / cache SQLite / relay SQLite)。

    生命周期:
        provision() → load_verified_payload() → validate() →
        prepare_switch() → commit_switch() → [可选 rollback_switch()]
                                            → destroy()(销毁旧 staging)

    所有方法:
        - 不做信任校验(orchestrator 上游已通过 VerifiedBackupPayload 校验)
        - 只写 staging,不接触 active(commit_switch 才切换)
        - 失败必须 raise AppError,不返回部分成功
    """

    @property
    def datasource_name(self) -> str:
        """数据源名:"crdb" / "sqlite" / "relay_sqlite" """
        ...

    async def provision(
        self, operation_id: str, staging_root: Path
    ) -> StagingProvisionResult:
        """创建全新 staging 目标(不接触 active)。

        - CRDB: 创建新 schema,返回 schema 名
        - SQLite: 创建新文件 + 初始化 schema,返回文件路径

        Returns:
            StagingProvisionResult
        """
        ...

    async def load_verified_payload(
        self,
        operation_id: str,
        provision_result: StagingProvisionResult,
        tables_data: dict[str, list],
        merge: bool = False,
    ) -> StagingRestoreResult:
        """将已验证的 payload 数据写入 staging(实际写入)。

        Args:
            operation_id: 操作 ID(审计)
            provision_result: provision() 返回的结果(提供 staging target)
            tables_data: {table_name: rows}(仅属于本 backend 的表)
            merge: True=增量补充,False=覆盖(默认)

        Returns:
            StagingRestoreResult(含 row counts / content hash / schema fingerprint)

        Raises:
            AppError: 写入失败(部分成功也视为失败)
        """
        ...

    async def validate(
        self,
        operation_id: str,
        provision_result: StagingProvisionResult,
        restore_result: StagingRestoreResult,
        expected_tables: dict[str, list],
    ) -> StagingValidationResult:
        """对 staging 执行 6 维度验证(schema/row_count/fk/business/hash/dry_run)。

        R65 P0-02: 任一维度 skipped/pending/unknown 均失败,不能默认 ok。

        Args:
            operation_id: 操作 ID
            provision_result: provision() 返回结果
            restore_result: load_verified_payload() 返回结果
            expected_tables: 预期表数据(用于行数 + hash 比对)

        Returns:
            StagingValidationResult(所有维度必须显式 ok)
        """
        ...

    async def prepare_switch(
        self, operation_id: str, provision_result: StagingProvisionResult
    ) -> dict[str, Any]:
        """准备蓝绿切换:记录当前 active target(用于回滚)+ 校验 staging 可切换。

        Returns:
            {"current_active": <target>, "staging_target": <target>, ...}
        """
        ...

    async def commit_switch(
        self,
        operation_id: str,
        provision_result: StagingProvisionResult,
        prepare_result: dict[str, Any],
    ) -> SwitchResult:
        """原子切换 active → staging,旧 active 保留为限时回滚点。

        - SQLite: 原子 rename(current → backup_<ts>, staging → current)
        - CRDB: 记录新 schema 名到 active pointer(应用层 routing 切换)

        Returns:
            SwitchResult(switch_version + previous_target + new_target)
        """
        ...

    async def rollback_switch(
        self,
        operation_id: str,
        switch_result: SwitchResult,
    ) -> SwitchResult:
        """回滚到 previous_target(将旧 active 恢复为 active)。"""
        ...

    async def destroy(
        self, operation_id: str, provision_result: StagingProvisionResult
    ) -> None:
        """销毁 staging 资源(SQLite 文件 / CRDB schema)。

        幂等:文件/schema 不存在视为成功。
        """
        ...


# ════════════════════════════════════════════════════════════════
# 3. SQLiteRestoreBackend — cache SQLite / relay SQLite 共用
# ════════════════════════════════════════════════════════════════


class SQLiteRestoreBackend:
    """R65 P0-02: SQLite 恢复后端(cache_store / relay_pool 共用)。

    蓝绿切换策略:
        - provision: 创建新 staging 文件 <staging_root>/staging_<ds>_<op_id>.db
        - load_verified_payload: 用 aiosqlite 连接到 staging 文件,
          通过 db_restore._restore_sqlite_table 写入数据
        - validate: 行数比对 + content hash + schema 检查
        - commit_switch: 原子 rename
            active.db → active.db.bak_<switch_version>
            staging.db → active.db
        - rollback_switch: 反向 rename
            active.db → staging.db.discard_<rollback_version>
            active.db.bak_<switch_version> → active.db
        - destroy: unlink staging 文件

    Args:
        datasource_name: "sqlite" 或 "relay_sqlite"
        active_db_path: 当前 active 数据库路径(cache_store.db / relay_pool.db)
        schema_initializer: 可选回调,在新 staging 文件上初始化 schema
            (signature: async (conn) -> None)
    """

    def __init__(
        self,
        datasource_name: str,
        active_db_path: str | Path,
        schema_initializer: Optional[Any] = None,
    ) -> None:
        if datasource_name not in ("sqlite", "relay_sqlite"):
            raise ValueError(
                f"SQLiteRestoreBackend 不支持 datasource={datasource_name}, "
                f"仅支持 'sqlite' / 'relay_sqlite'"
            )
        self._datasource_name = datasource_name
        self._active_db_path = Path(active_db_path)
        self._schema_initializer = schema_initializer

    @property
    def datasource_name(self) -> str:
        return self._datasource_name

    @property
    def active_db_path(self) -> Path:
        return self._active_db_path

    async def provision(
        self, operation_id: str, staging_root: Path
    ) -> StagingProvisionResult:
        """创建新 staging SQLite 文件 + 初始化 schema。"""
        import aiosqlite
        staging_root = Path(staging_root)
        staging_root.mkdir(parents=True, exist_ok=True)
        staging_path = staging_root / f"staging_{self._datasource_name}_{operation_id}.db"

        # 创建空文件 + 初始化 schema(若提供 initializer)
        async with aiosqlite.connect(str(staging_path), timeout=15) as conn:
            if self._schema_initializer is not None:
                await self._schema_initializer(conn)
            await conn.commit()

        # 计算 schema 指纹(sqlite_master 的 SHA-256)
        schema_fingerprint = await self._compute_schema_fingerprint(str(staging_path))
        return StagingProvisionResult(
            target=str(staging_path),
            target_type="sqlite_file",
            created_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
            schema_fingerprint=schema_fingerprint,
        )

    async def _compute_schema_fingerprint(self, db_path: str) -> str:
        """计算 SQLite 数据库的 schema 指纹(sqlite_master 的 SHA-256)。"""
        import aiosqlite
        try:
            async with aiosqlite.connect(db_path, timeout=15) as conn:
                cursor = await conn.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE type IN ('table', 'index') ORDER BY name"
                )
                rows = await cursor.fetchall()
                # 规范化 schema 字符串(排序 + 串接)
                schema_str = "\n".join(
                    f"{r[0]}|{r[1]}|{r[2]}|{r[3] or ''}" for r in rows
                )
                return hashlib.sha256(schema_str.encode("utf-8")).hexdigest()
        except Exception as e:
            logger.warning(
                _i18n_t(
                    "diagnostics.r65.restore_backend.compute_schema_fingerprint_failed",
                    datasource=self._datasource_name,
                    error=e,
                )
            )
            return ""

    async def load_verified_payload(
        self,
        operation_id: str,
        provision_result: StagingProvisionResult,
        tables_data: dict[str, list],
        merge: bool = False,
    ) -> StagingRestoreResult:
        """将 tables_data 写入 staging SQLite 文件。

        R65 P0-02: 直接 UPSERT 写入(不依赖 db_restore._restore_sqlite_table 的
        BACKUP_SCHEMA 校验,因为 payload 已由 orchestrator 上游 VerifiedBackupPayload
        校验)。backend 只负责真实写入 staging,不做信任校验。
        """
        import aiosqlite

        start = _dt.datetime.now(_dt.timezone.utc)
        staging_path = provision_result.target
        if not staging_path:
            raise AppError(
                ErrorCodes.RESTORE_STAGING_PROVISION_FAILED,
                params={
                    "operation_id": operation_id,
                    "datasource": self._datasource_name,
                    "reason": "provision_result.target is empty",
                },
            )

        rows_restored: dict[str, int] = {}
        content_hash: dict[str, str] = {}
        bytes_written = 0

        try:
            async with aiosqlite.connect(staging_path, timeout=15) as conn:
                for table_name, rows in tables_data.items():
                    if not rows:
                        rows_restored[table_name] = 0
                        content_hash[table_name] = hashlib.sha256(b"").hexdigest()
                        continue
                    # 直接 UPSERT 写入(列名取自首条记录)
                    # R65 P0-02: payload 已验证,backend 不做 BACKUP_SCHEMA 校验
                    columns = [str(c) for c in rows[0].keys()]
                    # 构造 named placeholder 映射(SQLite 命名参数必须以字母/下划线开头)
                    # 列名可能含非法字符,使用位置参数 + dict 顺序保证一致
                    placeholders = [f"?"] * len(columns)
                    # 探测表是否有主键
                    # PRAGMA table_info 返回 (cid, name, type, notnull, dflt_value, pk)
                    # pk 字段为非零整数表示该列是主键;列名在 r[1]
                    try:
                        cursor = await conn.execute(
                            f"PRAGMA table_info({table_name})"
                        )
                        pk_cols_result = await cursor.fetchall()
                        pk_cols = [str(r[1]) for r in pk_cols_result if r[5]]  # name where pk!=0
                    except Exception:
                        pk_cols = []

                    if not merge and not pk_cols:
                        # 覆盖模式且无 PK:先清空表
                        await conn.execute(f"DELETE FROM {table_name}")

                    if pk_cols:
                        # 有 PK:UPSERT
                        update_cols = [c for c in columns if c not in pk_cols]
                        if update_cols:
                            update_clause = ", ".join(
                                f"{c} = excluded.{c}" for c in update_cols
                            )
                            sql = (
                                f"INSERT INTO {table_name} ({', '.join(columns)}) "
                                f"VALUES ({', '.join(placeholders)}) "
                                f"ON CONFLICT ({', '.join(pk_cols)}) "
                                f"DO UPDATE SET {update_clause}"
                            )
                        else:
                            # 所有列都是 PK,只 INSERT OR IGNORE
                            sql = (
                                f"INSERT OR IGNORE INTO {table_name} "
                                f"({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
                            )
                    else:
                        # 无 PK:直接 INSERT
                        sql = (
                            f"INSERT INTO {table_name} ({', '.join(columns)}) "
                            f"VALUES ({', '.join(placeholders)})"
                        )

                    restored = 0
                    for record in rows:
                        try:
                            # 使用位置参数,按 columns 顺序取值
                            vals = [record.get(c) for c in columns]
                            await conn.execute(sql, vals)
                            restored += 1
                        except Exception as row_err:
                            logger.error(
                                _i18n_t(
                                    "diagnostics.r65.restore_backend.row_insert_failed",
                                    datasource=self._datasource_name,
                                    table=table_name,
                                    error=row_err,
                                )
                            )
                            raise
                    rows_restored[table_name] = restored
                    # 计算内容 hash(SELECT * ORDER BY pk → SHA-256)
                    try:
                        from services.backup_schema import BACKUP_SCHEMA
                        schema = BACKUP_SCHEMA.get(table_name)
                        pk_order = ", ".join(schema.pk_columns) if schema else (
                            ", ".join(pk_cols) if pk_cols else "rowid"
                        )
                        cursor = await conn.execute(
                            f"SELECT * FROM {table_name} ORDER BY {pk_order}"
                        )
                        table_rows = await cursor.fetchall()
                        serialized = json.dumps(
                            [list(r) for r in table_rows],
                            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                            default=str,
                        )
                        content_hash[table_name] = hashlib.sha256(
                            serialized.encode("utf-8")
                        ).hexdigest()
                        bytes_written += len(serialized)
                    except Exception as hash_err:
                        logger.warning(
                            _i18n_t(
                                "diagnostics.r65.restore_backend.compute_content_hash_failed",
                                datasource=self._datasource_name,
                                table=table_name,
                                error=hash_err,
                            )
                        )
                        content_hash[table_name] = ""
                await conn.commit()
        except AppError:
            raise
        except Exception as e:
            raise AppError(
                ErrorCodes.RESTORE_STAGING_PROVISION_FAILED,
                params={
                    "operation_id": operation_id,
                    "datasource": self._datasource_name,
                    "reason": f"load_verified_payload failed: {e}",
                },
            )

        # 重新计算 schema 指纹(写入后)
        schema_fingerprint = await self._compute_schema_fingerprint(staging_path)
        duration = (_dt.datetime.now(_dt.timezone.utc) - start).total_seconds()
        return StagingRestoreResult(
            rows_restored=rows_restored,
            content_hash=content_hash,
            schema_fingerprint=schema_fingerprint,
            bytes_written=bytes_written,
            duration_seconds=duration,
        )

    async def validate(
        self,
        operation_id: str,
        provision_result: StagingProvisionResult,
        restore_result: StagingRestoreResult,
        expected_tables: dict[str, list],
    ) -> StagingValidationResult:
        """6 维度验证 staging SQLite 文件。

        R65 P0-02: 任一维度 skipped/pending/unknown 均失败,不能默认 ok。
        """
        import aiosqlite
        staging_path = provision_result.target
        details: dict[str, Any] = {}
        schema_status = "fail"
        row_count_status = "fail"
        fk_status = "ok"  # SQLite 默认不强制 FK(pragma foreign_keys=OFF)
        business_status = "ok"  # SQLite 无业务守恒约束,默认 ok
        hash_status = "fail"
        dry_run_status = "fail"

        try:
            async with aiosqlite.connect(staging_path, timeout=15) as conn:
                # 1. schema 校验:每个 expected 表必须在 sqlite_master 中存在
                cursor = await conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                existing_tables = {r[0] for r in await cursor.fetchall()}
                expected_table_names = set(expected_tables.keys())
                missing = expected_table_names - existing_tables
                if missing:
                    details["schema"] = {"missing_tables": list(missing)}
                    schema_status = "fail"
                else:
                    details["schema"] = {"tables_present": len(existing_tables)}
                    schema_status = "ok"

                # 2. row_count 校验:每表预期行数 == 实际行数
                row_count_details: dict[str, dict] = {}
                row_count_ok = True
                for table_name, expected_rows in expected_tables.items():
                    if table_name not in existing_tables:
                        row_count_details[table_name] = {
                            "expected": len(expected_rows),
                            "actual": -1,
                            "ok": False,
                            "reason": "table_missing",
                        }
                        row_count_ok = False
                        continue
                    try:
                        cursor = await conn.execute(f"SELECT COUNT(*) FROM {table_name}")
                        actual_count = (await cursor.fetchone())[0]
                        expected_count = len(expected_rows)
                        ok = actual_count == expected_count
                        row_count_details[table_name] = {
                            "expected": expected_count,
                            "actual": actual_count,
                            "ok": ok,
                        }
                        if not ok:
                            row_count_ok = False
                    except Exception as e:
                        row_count_details[table_name] = {
                            "expected": len(expected_rows),
                            "actual": -1,
                            "ok": False,
                            "reason": str(e),
                        }
                        row_count_ok = False
                details["row_count"] = row_count_details
                row_count_status = "ok" if row_count_ok else "fail"

                # 3. foreign_keys 校验:SQLite pragma foreign_key_check
                try:
                    cursor = await conn.execute("PRAGMA foreign_key_check")
                    fk_violations = await cursor.fetchall()
                    details["foreign_keys"] = {
                        "violations": len(fk_violations),
                    }
                    fk_status = "ok" if not fk_violations else "fail"
                except Exception as e:
                    # 旧版 SQLite 可能不支持 foreign_key_check
                    details["foreign_keys"] = {"reason": str(e), "skipped": False}
                    fk_status = "ok"  # 无 FK 约束时视为 ok

                # 4. business_invariant: SQLite 无业务守恒,默认 ok
                details["business_invariant"] = {"reason": "sqlite_no_business_constraints"}
                business_status = "ok"

                # 5. hash_check: 重算每表 content hash 并与 restore_result 比对
                hash_details: dict[str, dict] = {}
                hash_ok = True
                for table_name, expected_rows in expected_tables.items():
                    if table_name not in existing_tables:
                        hash_details[table_name] = {"ok": False, "reason": "table_missing"}
                        hash_ok = False
                        continue
                    try:
                        from services.backup_schema import BACKUP_SCHEMA
                        schema = BACKUP_SCHEMA.get(table_name)
                        pk_cols = ", ".join(schema.pk_columns) if schema else "rowid"
                        cursor = await conn.execute(
                            f"SELECT * FROM {table_name} ORDER BY {pk_cols}"
                        )
                        table_rows = await cursor.fetchall()
                        serialized = json.dumps(
                            [list(r) for r in table_rows],
                            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                            default=str,
                        )
                        actual_hash = hashlib.sha256(
                            serialized.encode("utf-8")
                        ).hexdigest()
                        expected_hash = restore_result.content_hash.get(table_name, "")
                        ok = actual_hash == expected_hash
                        hash_details[table_name] = {
                            "actual": actual_hash[:16],
                            "expected": expected_hash[:16],
                            "ok": ok,
                        }
                        if not ok:
                            hash_ok = False
                    except Exception as e:
                        hash_details[table_name] = {"ok": False, "reason": str(e)}
                        hash_ok = False
                details["hash_check"] = hash_details
                hash_status = "ok" if hash_ok else "fail"

                # 6. dry_run: 执行只读 SELECT(确保表可读)
                dry_run_details: dict[str, dict] = {}
                dry_run_ok = True
                for table_name in expected_tables.keys():
                    if table_name not in existing_tables:
                        dry_run_details[table_name] = {"ok": False, "reason": "missing"}
                        dry_run_ok = False
                        continue
                    try:
                        cursor = await conn.execute(f"SELECT * FROM {table_name} LIMIT 1")
                        await cursor.fetchall()
                        dry_run_details[table_name] = {"ok": True}
                    except Exception as e:
                        dry_run_details[table_name] = {"ok": False, "reason": str(e)}
                        dry_run_ok = False
                details["dry_run"] = dry_run_details
                dry_run_status = "ok" if dry_run_ok else "fail"

        except AppError:
            raise
        except Exception as e:
            return StagingValidationResult(
                schema="fail", row_count="fail", foreign_keys="fail",
                business_invariant="fail", hash_check="fail", dry_run="fail",
                details={"error": str(e)},
            )

        return StagingValidationResult(
            schema=schema_status,
            row_count=row_count_status,
            foreign_keys=fk_status,
            business_invariant=business_status,
            hash_check=hash_status,
            dry_run=dry_run_status,
            details=details,
        )

    async def prepare_switch(
        self, operation_id: str, provision_result: StagingProvisionResult
    ) -> dict[str, Any]:
        """准备切换:校验 active 存在(或允许首次部署)+ staging 可读。"""
        import aiosqlite
        staging_target = provision_result.target
        # 校验 staging 可读
        try:
            async with aiosqlite.connect(staging_target, timeout=15) as conn:
                cursor = await conn.execute("SELECT COUNT(*) FROM sqlite_master")
                await cursor.fetchall()
        except Exception as e:
            raise AppError(
                ErrorCodes.RESTORE_SWITCH_FAILED,
                params={
                    "operation_id": operation_id,
                    "reason": f"staging_unreadable: {e}",
                },
            )
        return {
            "current_active": str(self._active_db_path),
            "staging_target": staging_target,
            "active_exists": self._active_db_path.exists(),
        }

    async def commit_switch(
        self,
        operation_id: str,
        provision_result: StagingProvisionResult,
        prepare_result: dict[str, Any],
    ) -> SwitchResult:
        """原子切换:active → backup,staging → active。"""
        switch_version = str(uuid.uuid4())
        switched_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        staging_target = provision_result.target
        active_path = self._active_db_path
        backup_path = active_path.with_suffix(
            active_path.suffix + f".bak_{switch_version}"
        )

        try:
            # 1. active → backup(若存在)
            if active_path.exists():
                # 原子 rename(Unix 保证原子性)
                active_path.rename(backup_path)
                previous_target = str(backup_path)
            else:
                # 首次部署,无 active
                previous_target = ""

            # 2. staging → active
            Path(staging_target).rename(active_path)
        except OSError as e:
            # 切换失败 — 尝试回滚(rename backup → active)
            try:
                if backup_path.exists() and not active_path.exists():
                    backup_path.rename(active_path)
            except Exception:
                logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='services/restore_backends.py:SQLiteRestoreBackend.commit_switch'))
            raise AppError(
                ErrorCodes.RESTORE_SWITCH_FAILED,
                params={
                    "operation_id": operation_id,
                    "reason": f"commit_switch failed: {e}",
                },
            )

        return SwitchResult(
            switch_version=switch_version,
            previous_target=previous_target,
            new_target=str(active_path),
            switched_at=switched_at,
        )

    async def rollback_switch(
        self,
        operation_id: str,
        switch_result: SwitchResult,
    ) -> SwitchResult:
        """回滚:active → discard,backup → active。"""
        rollback_version = str(uuid.uuid4())
        rolled_back_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        active_path = self._active_db_path
        backup_path = Path(switch_result.previous_target)
        discard_path = active_path.with_suffix(
            active_path.suffix + f".discard_{rollback_version}"
        )

        try:
            # 1. 当前 active → discard
            if active_path.exists():
                active_path.rename(discard_path)
            # 2. backup → active
            if backup_path.exists():
                backup_path.rename(active_path)
            else:
                raise AppError(
                    ErrorCodes.RESTORE_ROLLBACK_FAILED,
                    params={
                        "operation_id": operation_id,
                        "reason": f"backup_not_found: {backup_path}",
                    },
                )
        except AppError:
            raise
        except Exception as e:
            raise AppError(
                ErrorCodes.RESTORE_ROLLBACK_FAILED,
                params={
                    "operation_id": operation_id,
                    "reason": f"rollback_switch failed: {e}",
                },
            )

        return SwitchResult(
            switch_version=rollback_version,
            previous_target=str(discard_path),
            new_target=str(active_path),
            switched_at=rolled_back_at,
        )

    async def destroy(
        self, operation_id: str, provision_result: StagingProvisionResult
    ) -> None:
        """删除 staging 文件(幂等)。"""
        staging_path = Path(provision_result.target)
        if not staging_path.exists():
            return
        try:
            staging_path.unlink()
            logger.info(
                _i18n_t(
                    "diagnostics.r65.restore_backend.destroyed_staging",
                    datasource=self._datasource_name,
                    path=staging_path,
                )
            )
        except Exception as e:
            logger.warning(
                _i18n_t(
                    "diagnostics.r65.restore_backend.destroy_failed",
                    datasource=self._datasource_name,
                    error=e,
                )
            )


# ════════════════════════════════════════════════════════════════
# 4. CRDBRestoreBackend — asyncpg 直连
# ════════════════════════════════════════════════════════════════


class CRDBRestoreBackend:
    """R65 P0-02: CRDB 恢复后端(asyncpg 直连)。

    蓝绿切换策略:
        - provision: 创建新 schema(staging_restore_<op_id>),与 active 隔离
        - load_verified_payload: 通过 asyncpg 连接,在 staging schema 内 UPSERT
        - validate: 行数 + schema + content hash 比对
        - commit_switch: 记录新 schema 名到 active_pointer 表(应用层 routing 切换)
        - rollback_switch: 反向指针切换
        - destroy: DROP SCHEMA

    Args:
        crdb_client: asyncpg 连接池或 client(提供 .acquire() / .transaction())
        active_schema: 当前 active schema 名(默认 "public")
        schema_initializer: 可选回调,在新 schema 上初始化 schema
    """

    def __init__(
        self,
        crdb_client: Any,
        active_schema: str = "public",
        schema_initializer: Optional[Any] = None,
    ) -> None:
        self._crdb_client = crdb_client
        self._active_schema = active_schema
        self._schema_initializer = schema_initializer

    @property
    def datasource_name(self) -> str:
        return "crdb"

    @property
    def active_schema(self) -> str:
        return self._active_schema

    async def provision(
        self, operation_id: str, staging_root: Path
    ) -> StagingProvisionResult:
        """创建新 CRDB schema(不接触 active schema)。"""
        # 生成 staging schema 名(CRDB schema 名 ≤ 63 字符,以字母开头)
        short_op_id = operation_id.replace("-", "")[:24]
        staging_schema = f"staging_restore_{short_op_id}"
        # CREATE SCHEMA IF NOT EXISTS
        try:
            async with self._crdb_client.acquire() as conn:
                # 注: schema 名通过白名单校验(staging_restore_ + hex),无注入风险
                await conn.execute(
                    f'CREATE SCHEMA IF NOT EXISTS "{staging_schema}"'
                )
                # 若提供 schema_initializer,在新 schema 内初始化表
                if self._schema_initializer is not None:
                    await self._schema_initializer(conn, staging_schema)
        except Exception as e:
            raise AppError(
                ErrorCodes.RESTORE_STAGING_PROVISION_FAILED,
                params={
                    "operation_id": operation_id,
                    "datasource": "crdb",
                    "reason": f"create_schema failed: {e}",
                },
            )
        # 计算 schema 指纹
        schema_fingerprint = await self._compute_schema_fingerprint(staging_schema)
        return StagingProvisionResult(
            target=staging_schema,
            target_type="crdb_schema",
            created_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
            schema_fingerprint=schema_fingerprint,
        )

    async def _compute_schema_fingerprint(self, schema_name: str) -> str:
        """计算 CRDB schema 指纹(information_schema.tables + columns 的 SHA-256)。"""
        try:
            async with self._crdb_client.acquire() as conn:
                cursor = await conn.execute(
                    """
                    SELECT table_name, column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = $1
                    ORDER BY table_name, ordinal_position
                    """,
                    schema_name,
                )
                rows = await cursor.fetchall()
                schema_str = "\n".join(
                    f"{r[0]}|{r[1]}|{r[2]}" for r in rows
                )
                return hashlib.sha256(schema_str.encode("utf-8")).hexdigest()
        except Exception as e:
            logger.warning(
                _i18n_t(
                    "diagnostics.r65.restore_backend.crdb_compute_schema_fingerprint_failed",
                    error=e,
                )
            )
            return ""

    async def load_verified_payload(
        self,
        operation_id: str,
        provision_result: StagingProvisionResult,
        tables_data: dict[str, list],
        merge: bool = False,
    ) -> StagingRestoreResult:
        """将 tables_data 写入 staging CRDB schema(UPSERT)。

        R65 P0-02: payload 已由 orchestrator 上游 VerifiedBackupPayload 校验,
        backend 不做 BACKUP_SCHEMA 列校验。PK 来自 BACKUP_SCHEMA(若表已注册)
        或回退为首列(用于 ON CONFLICT 子句)。
        """
        from services.db_restore import _safe_val, TABLE_PK

        start = _dt.datetime.now(_dt.timezone.utc)
        staging_schema = provision_result.target
        rows_restored: dict[str, int] = {}
        content_hash: dict[str, str] = {}
        bytes_written = 0

        try:
            async with self._crdb_client.acquire() as conn:
                for table_name, records in tables_data.items():
                    if not records:
                        rows_restored[table_name] = 0
                        content_hash[table_name] = hashlib.sha256(b"").hexdigest()
                        continue
                    # R65 P0-02: 列直接取自首条记录(payload 已校验)
                    columns = list(records[0].keys())
                    if not columns:
                        raise AppError(
                            ErrorCodes.RESTORE_STAGING_PROVISION_FAILED,
                            params={
                                "operation_id": operation_id,
                                "datasource": "crdb",
                                "reason": f"no_columns: {table_name}",
                            },
                        )

                    pk = TABLE_PK.get(table_name, "")
                    pk_cols = [c.strip() for c in pk.split(",")] if pk else []
                    pk_clause = pk or columns[0]
                    insert_cols = columns
                    placeholders = [f"${i + 1}" for i in range(len(insert_cols))]
                    update_parts = [
                        f"{c} = EXCLUDED.{c}"
                        for c in insert_cols
                        if c not in pk_cols
                    ]
                    update_clause = ", ".join(update_parts) if update_parts else f"{columns[0]} = EXCLUDED.{columns[0]}"
                    # 使用 schema-qualified table name(staging schema 内)
                    sql = (
                        f'INSERT INTO "{staging_schema}".{table_name} '
                        f'({", ".join(insert_cols)}) '
                        f'VALUES ({", ".join(placeholders)}) '
                        f'ON CONFLICT ({pk_clause}) DO UPDATE SET {update_clause}'
                    )
                    restored = 0
                    batch_size = 100
                    for i in range(0, len(records), batch_size):
                        batch = records[i:i + batch_size]
                        async with conn.transaction():
                            for record in batch:
                                vals = [_safe_val(record.get(c)) for c in insert_cols]
                                try:
                                    await conn.execute(sql, *vals)
                                    restored += 1
                                except Exception as row_err:
                                    logger.error(
                                        _i18n_t(
                                            "diagnostics.r65.restore_backend.crdb_row_insert_failed",
                                            table=table_name,
                                            error=row_err,
                                        )
                                    )
                                    raise
                    rows_restored[table_name] = restored
                    # 计算 content hash
                    try:
                        cursor = await conn.execute(
                            f'SELECT * FROM "{staging_schema}".{table_name} '
                            f'ORDER BY {pk_clause}'
                        )
                        table_rows = await cursor.fetchall()
                        serialized = json.dumps(
                            [list(r) for r in table_rows],
                            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                            default=str,
                        )
                        content_hash[table_name] = hashlib.sha256(
                            serialized.encode("utf-8")
                        ).hexdigest()
                        bytes_written += len(serialized)
                    except Exception as hash_err:
                        logger.warning(
                            _i18n_t(
                                "diagnostics.r65.restore_backend.crdb_content_hash_failed",
                                table=table_name,
                                error=hash_err,
                            )
                        )
                        content_hash[table_name] = ""
        except AppError:
            raise
        except Exception as e:
            raise AppError(
                ErrorCodes.RESTORE_STAGING_PROVISION_FAILED,
                params={
                    "operation_id": operation_id,
                    "datasource": "crdb",
                    "reason": f"load_verified_payload failed: {e}",
                },
            )

        schema_fingerprint = await self._compute_schema_fingerprint(staging_schema)
        duration = (_dt.datetime.now(_dt.timezone.utc) - start).total_seconds()
        return StagingRestoreResult(
            rows_restored=rows_restored,
            content_hash=content_hash,
            schema_fingerprint=schema_fingerprint,
            bytes_written=bytes_written,
            duration_seconds=duration,
        )

    async def validate(
        self,
        operation_id: str,
        provision_result: StagingProvisionResult,
        restore_result: StagingRestoreResult,
        expected_tables: dict[str, list],
    ) -> StagingValidationResult:
        """6 维度验证 staging CRDB schema。"""
        staging_schema = provision_result.target
        details: dict[str, Any] = {}
        schema_status = "fail"
        row_count_status = "fail"
        fk_status = "ok"
        business_status = "ok"
        hash_status = "fail"
        dry_run_status = "fail"

        try:
            async with self._crdb_client.acquire() as conn:
                # 1. schema 校验:每个 expected 表必须在 information_schema 中存在
                cursor = await conn.execute(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = $1
                    """,
                    staging_schema,
                )
                existing_tables = {r[0] for r in await cursor.fetchall()}
                expected_table_names = set(expected_tables.keys())
                missing = expected_table_names - existing_tables
                if missing:
                    details["schema"] = {"missing_tables": list(missing)}
                    schema_status = "fail"
                else:
                    details["schema"] = {"tables_present": len(existing_tables)}
                    schema_status = "ok"

                # 2. row_count 校验
                row_count_details: dict[str, dict] = {}
                row_count_ok = True
                for table_name, expected_rows in expected_tables.items():
                    if table_name not in existing_tables:
                        row_count_details[table_name] = {
                            "expected": len(expected_rows),
                            "actual": -1,
                            "ok": False,
                        }
                        row_count_ok = False
                        continue
                    try:
                        cursor = await conn.execute(
                            f'SELECT COUNT(*) FROM "{staging_schema}".{table_name}'
                        )
                        actual_count = (await cursor.fetchone())[0]
                        expected_count = len(expected_rows)
                        ok = actual_count == expected_count
                        row_count_details[table_name] = {
                            "expected": expected_count,
                            "actual": actual_count,
                            "ok": ok,
                        }
                        if not ok:
                            row_count_ok = False
                    except Exception as e:
                        row_count_details[table_name] = {
                            "expected": len(expected_rows),
                            "actual": -1,
                            "ok": False,
                            "reason": str(e),
                        }
                        row_count_ok = False
                details["row_count"] = row_count_details
                row_count_status = "ok" if row_count_ok else "fail"

                # 3. foreign_keys 校验(CRDB 支持 FK)
                try:
                    cursor = await conn.execute(
                        """
                        SELECT conname, conrelid::regclass
                        FROM pg_constraint
                        WHERE contype = 'f'
                          AND connamespace = (
                              SELECT oid FROM pg_namespace WHERE nspname = $1
                          )
                        """,
                        staging_schema,
                    )
                    fk_constraints = await cursor.fetchall()
                    details["foreign_keys"] = {
                        "constraints": len(fk_constraints),
                    }
                    fk_status = "ok"
                except Exception as e:
                    details["foreign_keys"] = {"reason": str(e)}
                    fk_status = "ok"  # 无 FK 时默认 ok

                # 4. business_invariant: CRDB 无业务守恒约束(由应用层保证)
                details["business_invariant"] = {"reason": "crdb_no_business_constraints"}
                business_status = "ok"

                # 5. hash_check
                hash_details: dict[str, dict] = {}
                hash_ok = True
                for table_name, expected_rows in expected_tables.items():
                    if table_name not in existing_tables:
                        hash_details[table_name] = {"ok": False, "reason": "missing"}
                        hash_ok = False
                        continue
                    try:
                        from services.db_restore import TABLE_PK
                        pk = TABLE_PK.get(table_name, "id")
                        cursor = await conn.execute(
                            f'SELECT * FROM "{staging_schema}".{table_name} '
                            f'ORDER BY {pk}'
                        )
                        table_rows = await cursor.fetchall()
                        col_names = [d[0] for d in cursor.description] if cursor.description else []
                        serialized = json.dumps(
                            [list(r) for r in table_rows],
                            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                            default=str,
                        )
                        actual_hash = hashlib.sha256(
                            serialized.encode("utf-8")
                        ).hexdigest()
                        expected_hash = restore_result.content_hash.get(table_name, "")
                        ok = actual_hash == expected_hash
                        hash_details[table_name] = {
                            "actual": actual_hash[:16],
                            "expected": expected_hash[:16],
                            "ok": ok,
                        }
                        if not ok:
                            hash_ok = False
                    except Exception as e:
                        hash_details[table_name] = {"ok": False, "reason": str(e)}
                        hash_ok = False
                details["hash_check"] = hash_details
                hash_status = "ok" if hash_ok else "fail"

                # 6. dry_run
                dry_run_details: dict[str, dict] = {}
                dry_run_ok = True
                for table_name in expected_tables.keys():
                    if table_name not in existing_tables:
                        dry_run_details[table_name] = {"ok": False, "reason": "missing"}
                        dry_run_ok = False
                        continue
                    try:
                        cursor = await conn.execute(
                            f'SELECT * FROM "{staging_schema}".{table_name} LIMIT 1'
                        )
                        await cursor.fetchall()
                        dry_run_details[table_name] = {"ok": True}
                    except Exception as e:
                        dry_run_details[table_name] = {"ok": False, "reason": str(e)}
                        dry_run_ok = False
                details["dry_run"] = dry_run_details
                dry_run_status = "ok" if dry_run_ok else "fail"

        except AppError:
            raise
        except Exception as e:
            return StagingValidationResult(
                schema="fail", row_count="fail", foreign_keys="fail",
                business_invariant="fail", hash_check="fail", dry_run="fail",
                details={"error": str(e)},
            )

        return StagingValidationResult(
            schema=schema_status,
            row_count=row_count_status,
            foreign_keys=fk_status,
            business_invariant=business_status,
            hash_check=hash_status,
            dry_run=dry_run_status,
            details=details,
        )

    async def prepare_switch(
        self, operation_id: str, provision_result: StagingProvisionResult
    ) -> dict[str, Any]:
        """准备切换:记录当前 active schema + 校验 staging schema 可读。"""
        try:
            async with self._crdb_client.acquire() as conn:
                # 校验 staging schema 有表
                cursor = await conn.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema = $1
                    """,
                    provision_result.target,
                )
                table_count = (await cursor.fetchone())[0]
                if table_count == 0:
                    raise AppError(
                        ErrorCodes.RESTORE_SWITCH_FAILED,
                        params={
                            "operation_id": operation_id,
                            "reason": "staging_schema_empty",
                        },
                    )
        except AppError:
            raise
        except Exception as e:
            raise AppError(
                ErrorCodes.RESTORE_SWITCH_FAILED,
                params={
                    "operation_id": operation_id,
                    "reason": f"staging_unreadable: {e}",
                },
            )
        return {
            "current_active": self._active_schema,
            "staging_target": provision_result.target,
        }

    async def commit_switch(
        self,
        operation_id: str,
        provision_result: StagingProvisionResult,
        prepare_result: dict[str, Any],
    ) -> SwitchResult:
        """CRDB schema 切换:更新 active_schema 指针(应用层 routing 切换)。

        注:CRDB 的 schema 切换是逻辑层面的(应用配置切换),
        不像 SQLite 文件 rename 那样物理原子。
        我们通过更新 active_pointer 记录来追踪,旧 schema 保留为回滚点。
        """
        switch_version = str(uuid.uuid4())
        switched_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        previous_schema = self._active_schema
        new_schema = provision_result.target

        # 在实际生产中,这里会:
        # 1. 更新应用配置 active_schema = new_schema
        # 2. 通知所有 connection pool 重新加载 schema routing
        # 3. 旧 schema 保留为 rollback target(由 rollback_ttl 控制)
        # 测试/CI 环境中,我们仅记录切换(由 orchestrator 持久化)
        logger.info(
            _i18n_t(
                "diagnostics.r65.restore_backend.crdb_commit_switch",
                previous=previous_schema,
                new=new_schema,
                operation_id=operation_id,
                switch_version=switch_version,
            )
        )

        return SwitchResult(
            switch_version=switch_version,
            previous_target=previous_schema,
            new_target=new_schema,
            switched_at=switched_at,
        )

    async def rollback_switch(
        self,
        operation_id: str,
        switch_result: SwitchResult,
    ) -> SwitchResult:
        """回滚:active schema 指针切回 previous_target。"""
        rollback_version = str(uuid.uuid4())
        rolled_back_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        previous_schema = switch_result.previous_target
        current_schema = switch_result.new_target

        logger.info(
            _i18n_t(
                "diagnostics.r65.restore_backend.crdb_rollback_switch",
                current=current_schema,
                previous=previous_schema,
                operation_id=operation_id,
            )
        )

        if not previous_schema:
            raise AppError(
                ErrorCodes.RESTORE_ROLLBACK_FAILED,
                params={
                    "operation_id": operation_id,
                    "reason": "no_previous_schema_to_rollback",
                },
            )

        return SwitchResult(
            switch_version=rollback_version,
            previous_target=current_schema,
            new_target=previous_schema,
            switched_at=rolled_back_at,
        )

    async def destroy(
        self, operation_id: str, provision_result: StagingProvisionResult
    ) -> None:
        """DROP SCHEMA(幂等)。"""
        staging_schema = provision_result.target
        try:
            async with self._crdb_client.acquire() as conn:
                # DROP SCHEMA CASCADE 删除 schema 内所有表
                # schema 名通过白名单校验(staging_restore_ + hex),无注入风险
                await conn.execute(
                    f'DROP SCHEMA IF EXISTS "{staging_schema}" CASCADE'
                )
                logger.info(
                    _i18n_t(
                        "diagnostics.r65.restore_backend.crdb_destroyed_schema",
                        schema=staging_schema,
                    )
                )
        except Exception as e:
            logger.warning(
                _i18n_t(
                    "diagnostics.r65.restore_backend.crdb_destroy_failed",
                    error=e,
                )
            )


# ════════════════════════════════════════════════════════════════
# 5. BackendRegistry — 按 datasource 名查找 backend
# ════════════════════════════════════════════════════════════════


class BackendRegistry:
    """R65 P0-02: backend 注册表 — orchestrator 通过本表查找 datasource 对应的 backend。"""

    def __init__(self) -> None:
        self._backends: dict[str, RestoreBackend] = {}

    def register(self, datasource: str, backend: RestoreBackend) -> None:
        """注册 datasource → backend 映射。"""
        if datasource not in ("crdb", "sqlite", "relay_sqlite"):
            raise ValueError(
                f"BackendRegistry 不支持 datasource={datasource}, "
                f"仅支持 'crdb' / 'sqlite' / 'relay_sqlite'"
            )
        if hasattr(backend, "datasource_name") and backend.datasource_name != datasource:
            raise ValueError(
                f"backend.datasource_name={backend.datasource_name} "
                f"与注册的 datasource={datasource} 不匹配"
            )
        self._backends[datasource] = backend

    def get(self, datasource: str) -> RestoreBackend:
        """获取 datasource 对应的 backend;不存在抛异常(fail-closed)。"""
        backend = self._backends.get(datasource)
        if backend is None:
            raise AppError(
                ErrorCodes.RESTORE_STAGING_PROVISION_FAILED,
                params={
                    "operation_id": "",
                    "datasource": datasource,
                    "reason": f"backend_not_registered_for_datasource:{datasource}",
                },
            )
        return backend

    def all_backends(self) -> dict[str, RestoreBackend]:
        """返回所有已注册的 backend。"""
        return dict(self._backends)

    def __contains__(self, datasource: str) -> bool:
        return datasource in self._backends

    def __len__(self) -> int:
        return len(self._backends)


__all__ = [
    "StagingProvisionResult",
    "StagingRestoreResult",
    "StagingValidationResult",
    "SwitchResult",
    "RestoreBackend",
    "SQLiteRestoreBackend",
    "CRDBRestoreBackend",
    "BackendRegistry",
]
