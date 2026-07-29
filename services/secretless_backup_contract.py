"""R83 Secretless 精确备份合同验证与隔离 CockroachDB 恢复入口。

本模块只服务于 ``APP_ENV=test`` 且 ``SECRETLESS_MODE=true`` 的隔离发布门禁。
它不会修改全局 ``database.session._client``，也不会向源数据库写入数据。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from database.session import DDL_STATEMENTS, MIGRATION_STATEMENTS
from services.backup_dr_validate import (
    ExactBackupContract,
    validate_exact_backup_contract,
)
from services.backup_schema import (
    BACKUP_SCHEMA,
    get_tables_by_source,
    validate_columns_for_table,
)
from services.error_codes import AppError, ErrorCodes
from storage.r2 import R2Storage

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SCHEMA_VERSION = f"r36_{len(BACKUP_SCHEMA)}tables"


def _contract_error(reason: str, *, field: str = "") -> AppError:
    return AppError(
        ErrorCodes.SECRETLESS_CONTRACT_VIOLATION,
        params={
            "component": "backup_restore",
            "reason": reason,
            "field": field,
        },
    )


def _require_secretless_boundary() -> None:
    if os.environ.get("APP_ENV", "").lower() != "test":
        raise _contract_error("SECRETLESS_RESTORE_APP_ENV_INVALID", field="APP_ENV")
    if os.environ.get("SECRETLESS_MODE", "").lower() not in {"1", "true", "yes"}:
        raise _contract_error("SECRETLESS_RESTORE_MODE_INVALID", field="SECRETLESS_MODE")


def _safe_identifier(value: str) -> str:
    normalized = value.strip().lower()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise _contract_error("SECRETLESS_RESTORE_DATABASE_IDENTIFIER_INVALID", field="database")
    return normalized


def _quote_identifier(value: str) -> str:
    return f'"{_safe_identifier(value)}"'


def _database_identity(dsn: str) -> str:
    return hashlib.sha256(dsn.encode()).hexdigest()[:16]


def _error_code(exc: BaseException) -> str:
    """从受控异常中提取稳定错误码，不把任意文本伪装成错误分类。"""
    if isinstance(exc, AppError):
        reason = str(exc.params.get("reason", "")).strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_.-]{2,159}", reason):
            return reason
        return exc.code
    message = str(exc).strip()
    candidate = message.split(":", 1)[0].strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_.-]{2,159}", candidate):
        return candidate
    return ErrorCodes.SECRETLESS_INTERNAL_UNEXPECTED


def _dsn_with_database(dsn: str, database: str) -> str:
    safe_database = _safe_identifier(database)
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.netloc:
        raise _contract_error("SECRETLESS_RESTORE_SOURCE_DSN_INVALID", field="source_dsn")
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{safe_database}", parsed.query, ""))


def _canonical_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return value
            return _canonical_value(decoded)
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _rows_digest(rows: list[dict[str, Any]], pk_columns: tuple[str, ...]) -> str:
    normalized = [_canonical_value(dict(row)) for row in rows]
    normalized.sort(
        key=lambda row: json.dumps(
            [row.get(column) for column in pk_columns],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _schema_fingerprint() -> str:
    parts = []
    for table_name in sorted(BACKUP_SCHEMA):
        schema = BACKUP_SCHEMA[table_name]
        parts.append({
            "name": schema.name,
            "source": schema.source,
            "pk_columns": list(schema.pk_columns),
            "columns": list(schema.columns),
            "is_large": schema.is_large,
            "where_clause": schema.where_clause,
        })
    canonical = json.dumps(
        parts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()[:16]


def _storage_from_env() -> R2Storage:
    endpoint = os.environ.get("S3_ENDPOINT_URL", "")
    bucket = os.environ.get("S3_BUCKET_NAME", "")
    access_key = os.environ.get("S3_ACCESS_KEY_ID", "")
    secret_key = os.environ.get("S3_SECRET_ACCESS_KEY", "")
    if not all((endpoint, bucket, access_key, secret_key)):
        raise _contract_error("SECRETLESS_RESTORE_S3_CONFIG_INVALID", field="s3")
    store = R2Storage()
    store.configure(
        account_id="",
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        endpoint=endpoint,
    )
    return store


def _json_value(value: Any) -> Any:
    """将只读合同视图递归还原为 JSON 值，禁止 ``default=str`` 静默降级。"""
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported contract evidence value: {type(value).__name__}")


def _contract_document(contract: ExactBackupContract) -> dict[str, Any]:
    return {
        "schema_version": "secretless-exact-backup-contract/v1",
        "status": "success" if contract.valid else "failure",
        "valid": contract.valid,
        "backup_id": contract.backup_id,
        "payload_key": contract.payload_key,
        "manifest_key": contract.manifest_key,
        "complete_key": contract.complete_key,
        "manifest_sha256": contract.manifest_sha256,
        "ciphertext_sha256": contract.ciphertext_sha256,
        "plaintext_sha256": contract.plaintext_sha256,
        "backup_schema_version": contract.schema_version,
        "source_sha": contract.source_sha,
        "source_database_identity": contract.source_database_identity,
        "schema_fingerprint": contract.schema_fingerprint,
        "table_stats": _json_value(contract.manifest.get("table_stats", {})),
        "error_code": contract.error_code,
        "error_message": contract.error_message,
    }


def _write_json(path: Path | None, document: dict[str, Any]) -> None:
    encoded = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(path)
    print(json.dumps(document, ensure_ascii=False, sort_keys=True))


async def _validate(args: argparse.Namespace) -> ExactBackupContract:
    store = _storage_from_env()
    signing_key = os.environ.get("BACKUP_SIGNING_KEY", "").encode()
    if not signing_key:
        raise _contract_error("BACKUP_SIGNING_KEY_MISSING", field="BACKUP_SIGNING_KEY")
    await store.connect()
    try:
        return await validate_exact_backup_contract(
            backup_id=args.backup_id,
            payload_key=args.payload_key,
            manifest_key=args.manifest_key,
            complete_key=args.complete_key,
            backup_type="full",
            r2_storage=store,
            signing_key=signing_key,
            current_schema_version=args.current_schema_version,
            payload_read_key=getattr(args, "payload_read_key", ""),
        )
    finally:
        await store.close()


async def _capture_table_snapshot(
    pool: asyncpg.Pool,
    tables: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    async with pool.acquire() as connection:
        for table_name, payload_rows in sorted(tables.items()):
            schema = BACKUP_SCHEMA[table_name]
            payload_columns = list(payload_rows[0]) if payload_rows else list(schema.columns)
            columns = validate_columns_for_table(table_name, payload_columns)
            if not columns:
                columns = list(schema.pk_columns)
            column_sql = ", ".join(_quote_identifier(column) for column in columns)
            order_sql = ", ".join(_quote_identifier(column) for column in schema.pk_columns)
            records = await connection.fetch(
                f"SELECT {column_sql} FROM {_quote_identifier(table_name)} ORDER BY {order_sql}"
            )
            rows = [dict(record) for record in records]
            snapshot[table_name] = {
                "row_count": len(rows),
                "field_hash": _rows_digest(rows, schema.pk_columns),
                "columns": columns,
            }
    return snapshot


async def _initialize_target(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as connection:
        for statement in DDL_STATEMENTS:
            await connection.execute(statement)
        for statement in MIGRATION_STATEMENTS:
            try:
                await connection.execute(statement)
            except (asyncpg.DuplicateColumnError, asyncpg.DuplicateObjectError):
                continue


async def _target_schema_snapshot(pool: asyncpg.Pool) -> dict[str, list[str]]:
    """读取目标 public schema 的实际表/列，用于独立验证 schema 已创建。"""
    async with pool.acquire() as connection:
        records = await connection.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position"
        )
    snapshot: dict[str, list[str]] = {}
    for record in records:
        snapshot.setdefault(str(record["table_name"]), []).append(
            str(record["column_name"])
        )
    return snapshot


async def _target_user_table_count(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as connection:
        value = await connection.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
    return int(value or 0)


async def _target_column_types(pool: asyncpg.Pool) -> dict[str, dict[str, str]]:
    """读取目标列类型，恢复时不依赖 asyncpg 的隐式类型猜测。"""
    async with pool.acquire() as connection:
        records = await connection.fetch(
            "SELECT table_name, column_name, data_type, udt_name "
            "FROM information_schema.columns WHERE table_schema = 'public' "
            "ORDER BY table_name, ordinal_position"
        )
    result: dict[str, dict[str, str]] = {}
    for record in records:
        data_type = str(record["data_type"]).lower()
        udt_name = str(record["udt_name"]).lower()
        result.setdefault(str(record["table_name"]), {})[
            str(record["column_name"])
        ] = udt_name or data_type
    return result


def _coerce_restore_value(value: Any, column_type: str) -> Any:
    """把 JSON payload 值确定性转换为目标 CockroachDB 列类型。"""
    if value is None:
        return None
    normalized_type = column_type.lower()
    if normalized_type in {"json", "jsonb"}:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = str(value)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    if normalized_type in {"bool", "boolean"}:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in {
            "true", "false", "1", "0",
        }:
            return value.strip().lower() in {"true", "1"}
        raise ValueError(f"invalid boolean restore value: {value!r}")
    if normalized_type in {"numeric", "decimal"}:
        try:
            return value if isinstance(value, Decimal) else Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"invalid numeric restore value: {value!r}") from exc
    if normalized_type in {"uuid"}:
        try:
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"invalid UUID restore value: {value!r}") from exc
    if normalized_type in {"timestamp", "timestamptz"}:
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid timestamp restore value: {value!r}") from exc
    if normalized_type == "date":
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError(f"invalid date restore value: {value!r}") from exc
    if normalized_type in {"time", "timetz"}:
        if isinstance(value, time):
            return value
        try:
            return time.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid time restore value: {value!r}") from exc
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


async def _restore_crdb_payload(
    pool: asyncpg.Pool,
    tables: dict[str, list[dict[str, Any]]],
) -> dict[str, int]:
    restored: dict[str, int] = {}
    column_types = await _target_column_types(pool)
    async with pool.acquire() as connection, connection.transaction():
        for table_name, rows in sorted(
            tables.items(), key=lambda item: BACKUP_SCHEMA[item[0]].backup_order
        ):
            if not rows:
                restored[table_name] = 0
                continue
            raw_columns = list(rows[0])
            columns = validate_columns_for_table(table_name, raw_columns)
            if set(columns) != set(raw_columns):
                raise _contract_error("RESTORE_PAYLOAD_COLUMNS_INVALID", field=table_name)
            for row in rows:
                if set(row) != set(raw_columns):
                    raise _contract_error("RESTORE_PAYLOAD_ROW_COLUMNS_DRIFT", field=table_name)
            column_sql = ", ".join(_quote_identifier(column) for column in columns)
            placeholders = ", ".join(f"${index + 1}" for index in range(len(columns)))
            insert_sql = (
                f"INSERT INTO {_quote_identifier(table_name)} ({column_sql}) "
                f"VALUES ({placeholders})"
            )
            table_column_types = column_types.get(table_name, {})
            missing_types = sorted(set(columns) - set(table_column_types))
            if missing_types:
                raise _contract_error("RESTORE_TARGET_COLUMN_TYPE_MISSING", field=table_name)
            for row in rows:
                values = [
                    _coerce_restore_value(row.get(column), table_column_types[column])
                    for column in columns
                ]
                await connection.execute(insert_sql, *values)
            restored[table_name] = len(rows)
    return restored


async def _drop_target(source_dsn: str, target_database: str) -> None:
    connection = await asyncpg.connect(source_dsn, command_timeout=30)
    try:
        await connection.execute(f"DROP DATABASE IF EXISTS {_quote_identifier(target_database)} CASCADE")
    finally:
        await connection.close()


async def _restore(args: argparse.Namespace) -> dict[str, Any]:
    contract = await _validate(args)
    if not contract.valid:
        return _contract_document(contract)

    try:
        payload = json.loads(contract.plaintext_bytes)
    except json.JSONDecodeError as exc:
        raise _contract_error("BACKUP_RESTORE_PLAINTEXT_JSON_INVALID", field="payload") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tables"), dict):
        raise _contract_error("BACKUP_RESTORE_PLAINTEXT_SCHEMA_INVALID", field="tables")

    source_dsn = args.source_dsn or os.environ.get("SECRETLESS_CRDB_URL", "")
    if not source_dsn:
        raise _contract_error("SECRETLESS_CRDB_URL_MISSING", field="SECRETLESS_CRDB_URL")
    source_identity = _database_identity(source_dsn)
    if source_identity != contract.source_database_identity:
        raise _contract_error("SOURCE_IDENTITY_MISMATCH", field="source_identity")
    local_schema_fingerprint = _schema_fingerprint()
    if local_schema_fingerprint != contract.schema_fingerprint:
        raise _contract_error("SCHEMA_FINGERPRINT_MISMATCH", field="schema_fingerprint")

    allowed_crdb_tables = set(get_tables_by_source("crdb"))
    payload_tables = payload["tables"]
    crdb_tables: dict[str, list[dict[str, Any]]] = {}
    for table_name, rows in payload_tables.items():
        if table_name not in allowed_crdb_tables:
            continue
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise _contract_error("BACKUP_RESTORE_CRDB_ROWS_INVALID", field=table_name)
        crdb_tables[table_name] = rows
    if not crdb_tables:
        raise _contract_error("BACKUP_RESTORE_NO_CRDB_TABLES", field="tables")

    manifest_table_stats = contract.manifest.get("table_stats", {})
    if not isinstance(manifest_table_stats, Mapping):
        raise _contract_error("BACKUP_RESTORE_TABLE_STATS_INVALID", field="table_stats")
    for table_name, rows in crdb_tables.items():
        stats = manifest_table_stats.get(table_name)
        if not isinstance(stats, Mapping):
            raise _contract_error("BACKUP_RESTORE_TABLE_STATS_MISSING", field=table_name)
        if stats.get("source") != "crdb" or stats.get("row_count") != len(rows):
            raise _contract_error("BACKUP_RESTORE_TABLE_STATS_MISMATCH", field=table_name)

    run_component = re.sub(r"[^a-z0-9]", "", os.environ.get("GITHUB_RUN_ID", ""))[-12:]
    target_database = _safe_identifier(
        args.target_database
        or f"tgjiema_restore_{run_component or 'local'}_{uuid.uuid4().hex[:10]}"
    )
    target_dsn = _dsn_with_database(source_dsn, target_database)
    target_identity = _database_identity(target_dsn)
    if target_identity == source_identity:
        raise _contract_error("RESTORE_TARGET_IDENTITY_COLLISION", field="target_identity")

    source_pool = await asyncpg.create_pool(
        source_dsn, min_size=0, max_size=1, command_timeout=30
    )
    target_pool: asyncpg.Pool | None = None
    target_created = False
    try:
        source_before = await _capture_table_snapshot(source_pool, crdb_tables)
        async with source_pool.acquire() as connection:
            await connection.execute(f"CREATE DATABASE {_quote_identifier(target_database)}")
        target_created = True
        target_pool = await asyncpg.create_pool(
            target_dsn, min_size=0, max_size=1, command_timeout=30
        )
        target_tables_before = await _target_user_table_count(target_pool)
        if target_tables_before != 0:
            raise _contract_error("RESTORE_TARGET_NOT_BLANK", field="target_database")

        await _initialize_target(target_pool)
        target_schema = await _target_schema_snapshot(target_pool)
        schema_mismatches = []
        for table_name, rows in crdb_tables.items():
            expected_columns = set(rows[0]) if rows else set(BACKUP_SCHEMA[table_name].columns)
            actual_columns = set(target_schema.get(table_name, []))
            missing_columns = sorted(expected_columns - actual_columns)
            if missing_columns:
                schema_mismatches.append({
                    "table": table_name,
                    "missing_columns": missing_columns,
                })
        if schema_mismatches:
            raise _contract_error("RESTORE_TARGET_SCHEMA_MISMATCH", field="target_schema")
        restored = await _restore_crdb_payload(target_pool, crdb_tables)
        target_after = await _capture_table_snapshot(target_pool, crdb_tables)
        source_after = await _capture_table_snapshot(source_pool, crdb_tables)
        payload_snapshot = {
            table_name: {
                "row_count": len(rows),
                "field_hash": _rows_digest(rows, BACKUP_SCHEMA[table_name].pk_columns),
                "columns": (
                    list(rows[0]) if rows else list(BACKUP_SCHEMA[table_name].columns)
                ),
            }
            for table_name, rows in sorted(crdb_tables.items())
        }
        integrity_mismatches = []
        for table_name, expected in payload_snapshot.items():
            actual = target_after[table_name]
            if expected["row_count"] != actual["row_count"]:
                integrity_mismatches.append(f"{table_name}:row_count")
            if expected["field_hash"] != actual["field_hash"]:
                integrity_mismatches.append(f"{table_name}:field_hash")
        source_unchanged = source_before == source_after
        if integrity_mismatches:
            raise _contract_error("RESTORE_TARGET_INTEGRITY_MISMATCH", field="target_data")
        if not source_unchanged:
            raise _contract_error("RESTORE_SOURCE_CHANGED", field="source_database")

        async with target_pool.acquire() as connection:
            business_probe = int(
                await connection.fetchval(
                    f"SELECT count(*) FROM {_quote_identifier(next(iter(sorted(crdb_tables))))}"
                )
                or 0
            )

        return {
            **_contract_document(contract),
            "schema_version": "secretless-crdb-restore/v1",
            "status": "success",
            "operation_id": args.operation_id,
            "source_identity": source_identity,
            "target_identity": target_identity,
            "source_database": urlsplit(source_dsn).path.lstrip("/"),
            "target_database": target_database,
            "target_dsn_sha256": hashlib.sha256(target_dsn.encode()).hexdigest(),
            "target_before": {"user_table_count": target_tables_before, "blank": True},
            "target_schema": target_schema,
            "target_after": target_after,
            "payload_snapshot": payload_snapshot,
            "source_before": source_before,
            "source_after": source_after,
            "source_unchanged": source_unchanged,
            "restored_rows": restored,
            "schema_fingerprint_verified": True,
            "manifest_digest_verified": True,
            "payload_digest_verified": True,
            "complete_marker_verified": True,
            "business_probe": {"status": "pass", "row_count": business_probe},
        }
    except Exception:
        if target_pool is not None:
            await target_pool.close()
            target_pool = None
        if target_created:
            await _drop_target(source_dsn, target_database)
        raise
    finally:
        if target_pool is not None:
            await target_pool.close()
        await source_pool.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "restore-crdb"):
        child = subparsers.add_parser(command)
        child.add_argument("--backup-id", required=True)
        child.add_argument("--payload-key", required=True)
        child.add_argument("--manifest-key", required=True)
        child.add_argument("--complete-key", required=True)
        child.add_argument(
            "--payload-read-key",
            default="",
            help="corruption negative 专用损坏副本 key；权威 payload key 不变",
        )
        child.add_argument("--current-schema-version", default=_SCHEMA_VERSION)
        child.add_argument("--output-json", type=Path)
        if command == "restore-crdb":
            child.add_argument("--source-dsn", default="")
            child.add_argument("--target-database", default="")
            child.add_argument("--operation-id", required=True)
    return parser


async def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    _require_secretless_boundary()
    if args.command == "validate":
        contract = await _validate(args)
        document = _contract_document(contract)
        return (0 if contract.valid else 1), document
    document = await _restore(args)
    return (0 if document.get("status") == "success" else 1), document


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        returncode, document = asyncio.run(_run(args))
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        AppError,
        asyncpg.PostgresError,
    ) as exc:
        document = {
            "schema_version": "secretless-backup-contract-error/v1",
            "status": "failure",
            "error_code": _error_code(exc),
            "error_message": f"{type(exc).__name__}: {exc}",
        }
        returncode = 1
    _write_json(args.output_json, document)
    return returncode


if __name__ == "__main__":
    sys.exit(main())
