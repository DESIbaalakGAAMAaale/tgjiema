#!/usr/bin/env python3
"""R83 Step 13: current-SHA-bound Secretless CRDB switch/rollback executor.

The active pointer is a durable row in the source CockroachDB database.  A switch
is accepted only when the restored target database identity and minimal business
snapshot match the current restore-state contract.  Rollback uses compare-and-
swap semantics and proves that the source identity and business read are restored.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from services.error_codes import AppError, ErrorCodes

_POINTER_TABLE = "secretless_active_database_pointer"
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_CRDB_CONNECTION_OPTIONS = {
    "command_timeout": 30,
    # CockroachDB v24.1 rejects a new prepared statement while an asyncpg
    # portal is still active unless a preview session flag is enabled.  This
    # short-lived release executor uses the simple protocol instead of
    # weakening the database contract with a preview-only server setting.
    "statement_cache_size": 0,
}


def _contract_error(reason: str, *, field: str = "") -> AppError:
    return AppError(
        ErrorCodes.SECRETLESS_CONTRACT_VIOLATION,
        params={
            "component": "switch_rollback",
            "reason": reason,
            "field": field,
        },
    )


def _require_secretless_boundary() -> None:
    if os.environ.get("APP_ENV", "").lower() != "test":
        raise _contract_error("SECRETLESS_SWITCH_APP_ENV_REQUIRED", field="APP_ENV")
    if os.environ.get("SECRETLESS_MODE", "").lower() not in {"1", "true", "yes"}:
        raise _contract_error("SECRETLESS_SWITCH_MODE_REQUIRED", field="SECRETLESS_MODE")


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise _contract_error("SECRETLESS_SWITCH_DATABASE_IDENTIFIER_INVALID", field="database")
    return value


def _quote_identifier(value: str) -> str:
    return f'"{_safe_identifier(value)}"'


def _dsn_with_database(dsn: str, database: str) -> str:
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        raise _contract_error("SECRETLESS_SWITCH_SOURCE_DSN_INVALID", field="source_dsn")
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{_safe_identifier(database)}", parsed.query, ""))


def _database_identity(dsn: str) -> str:
    return hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:16]


def _load_state(path: Path) -> dict[str, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _contract_error("SECRETLESS_SWITCH_STATE_INVALID", field="state_file") from exc
    required = (
        "schema_version",
        "head_sha",
        "backup_id",
        "operation_id",
        "source_identity",
        "target_identity",
        "source_database",
        "target_database",
        "target_dsn_sha256",
    )
    if not isinstance(document, dict) or any(
        not str(document.get(field, "")).strip() for field in required
    ):
        raise _contract_error("SECRETLESS_SWITCH_STATE_INCOMPLETE", field="state_file")
    if document["schema_version"] != "secretless-restore-state/v1":
        raise _contract_error("SECRETLESS_SWITCH_STATE_SCHEMA_INVALID", field="schema_version")
    return {field: str(document[field]).strip() for field in required}


def _validate_state(state: dict[str, str], source_dsn: str) -> tuple[str, str]:
    current_sha = (os.environ.get("GITHUB_SHA", "") or state["head_sha"]).strip()
    if state["head_sha"] != current_sha:
        raise _contract_error("SECRETLESS_SWITCH_HEAD_SHA_MISMATCH", field="head_sha")
    source_database = _safe_identifier(state["source_database"])
    target_database = _safe_identifier(state["target_database"])
    runtime_source_database = urlsplit(source_dsn).path.lstrip("/")
    if runtime_source_database != source_database:
        raise _contract_error("SECRETLESS_SWITCH_SOURCE_DATABASE_MISMATCH", field="source_database")
    target_dsn = _dsn_with_database(source_dsn, target_database)
    if _database_identity(source_dsn) != state["source_identity"]:
        raise _contract_error("SECRETLESS_SWITCH_SOURCE_IDENTITY_MISMATCH", field="source_identity")
    if _database_identity(target_dsn) != state["target_identity"]:
        raise _contract_error("SECRETLESS_SWITCH_TARGET_IDENTITY_MISMATCH", field="target_identity")
    if hashlib.sha256(target_dsn.encode("utf-8")).hexdigest() != state["target_dsn_sha256"]:
        raise _contract_error("SECRETLESS_SWITCH_TARGET_DSN_DIGEST_MISMATCH", field="target_dsn_sha256")
    if state["source_identity"] == state["target_identity"]:
        raise _contract_error("SECRETLESS_SWITCH_IDENTITY_COLLISION", field="target_identity")
    return source_database, target_dsn


async def _business_snapshot(pool: asyncpg.Pool) -> dict[str, Any]:
    async with pool.acquire() as connection:
        records = await connection.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE' "
            "ORDER BY table_name"
        )
        tables = [
            str(record["table_name"])
            for record in records
            if str(record["table_name"]) != _POINTER_TABLE
        ]
        if not tables:
            raise _contract_error("SECRETLESS_SWITCH_BUSINESS_TABLE_MISSING", field="tables")
        counts: dict[str, int] = {}
        for table in tables:
            count_records = await connection.fetch(
                f"SELECT count(*) AS row_count FROM {_quote_identifier(table)}"
            )
            if len(count_records) != 1:
                raise _contract_error(
                    "SECRETLESS_SWITCH_BUSINESS_COUNT_INVALID",
                    field=table,
                )
            counts[table] = int(count_records[0]["row_count"] or 0)
    canonical = json.dumps(counts, sort_keys=True, separators=(",", ":")).encode()
    return {
        "status": "pass",
        "table_count": len(tables),
        "total_rows": sum(counts.values()),
        "row_counts_sha256": hashlib.sha256(canonical).hexdigest(),
    }


async def _ensure_pointer(connection: asyncpg.Connection, state: dict[str, str]) -> None:
    await connection.execute(
        f"CREATE TABLE IF NOT EXISTS {_quote_identifier(_POINTER_TABLE)} ("
        "singleton BOOL PRIMARY KEY DEFAULT true CHECK (singleton), "
        "active_database STRING NOT NULL, active_identity STRING NOT NULL, "
        "version INT8 NOT NULL, head_sha STRING NOT NULL, "
        "operation_id STRING NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    await connection.execute(
        f"INSERT INTO {_quote_identifier(_POINTER_TABLE)} "
        "(singleton, active_database, active_identity, version, head_sha, operation_id) "
        "VALUES (true, $1, $2, 0, $3, $4) ON CONFLICT (singleton) DO NOTHING",
        state["source_database"],
        state["source_identity"],
        state["head_sha"],
        state["operation_id"],
    )


async def _read_pointer(connection: asyncpg.Connection) -> dict[str, Any]:
    rows = await connection.fetch(
        f"SELECT active_database, active_identity, version, head_sha, operation_id "
        f"FROM {_quote_identifier(_POINTER_TABLE)} WHERE singleton=true"
    )
    if len(rows) != 1:
        raise _contract_error("SECRETLESS_SWITCH_POINTER_MISSING", field="active_pointer")
    return dict(rows[0])


async def _cas_pointer(
    connection: asyncpg.Connection,
    *,
    expected: dict[str, Any],
    database: str,
    identity: str,
    state: dict[str, str],
) -> dict[str, Any]:
    result = await connection.execute(
        f"UPDATE {_quote_identifier(_POINTER_TABLE)} SET "
        "active_database=$1, active_identity=$2, version=version+1, "
        "head_sha=$3, operation_id=$4, updated_at=now() "
        "WHERE singleton=true AND version=$5 AND active_database=$6 "
        "AND active_identity=$7 AND head_sha=$8 AND operation_id=$9",
        database,
        identity,
        state["head_sha"],
        state["operation_id"],
        int(expected["version"]),
        str(expected["active_database"]),
        str(expected["active_identity"]),
        str(expected["head_sha"]),
        str(expected["operation_id"]),
    )
    if result != "UPDATE 1":
        raise _contract_error("SECRETLESS_SWITCH_POINTER_CAS_FAILED", field="active_pointer")
    return await _read_pointer(connection)


async def _connect_pools(source_dsn: str, target_dsn: str) -> tuple[asyncpg.Pool, asyncpg.Pool]:
    source_pool = await asyncpg.create_pool(
        source_dsn,
        min_size=0,
        max_size=1,
        **_CRDB_CONNECTION_OPTIONS,
    )
    try:
        target_pool = await asyncpg.create_pool(
            target_dsn,
            min_size=0,
            max_size=1,
            **_CRDB_CONNECTION_OPTIONS,
        )
    except Exception:
        await source_pool.close()
        raise
    return source_pool, target_pool


async def _switch(state: dict[str, str], source_dsn: str) -> dict[str, Any]:
    _, target_dsn = _validate_state(state, source_dsn)
    source_pool, target_pool = await _connect_pools(source_dsn, target_dsn)
    try:
        source_probe = await _business_snapshot(source_pool)
        target_probe = await _business_snapshot(target_pool)
        async with source_pool.acquire() as connection, connection.transaction():
            await _ensure_pointer(connection, state)
            before = await _read_pointer(connection)
            if (
                before["active_database"] != state["source_database"]
                or before["active_identity"] != state["source_identity"]
                or before["head_sha"] != state["head_sha"]
                or before["operation_id"] != state["operation_id"]
            ):
                raise _contract_error("SECRETLESS_SWITCH_POINTER_SOURCE_STATE_INVALID", field="active_pointer")
            after = await _cas_pointer(
                connection,
                expected=before,
                database=state["target_database"],
                identity=state["target_identity"],
                state=state,
            )
        if after["active_identity"] != state["target_identity"]:
            raise _contract_error("SECRETLESS_SWITCH_TARGET_NOT_ACTIVE", field="active_identity")
        return {
            "schema_version": "secretless-switch-contract/v1",
            "status": "success",
            "action": "switch",
            "head_sha": state["head_sha"],
            "operation_id": state["operation_id"],
            "source_identity": state["source_identity"],
            "target_identity": state["target_identity"],
            "active_before": before,
            "active_after": after,
            "source_business_probe": source_probe,
            "target_business_probe": target_probe,
        }
    finally:
        await target_pool.close()
        await source_pool.close()


async def _probe(state: dict[str, str], source_dsn: str, inject_http_status: int) -> dict[str, Any]:
    _, target_dsn = _validate_state(state, source_dsn)
    source_pool, target_pool = await _connect_pools(source_dsn, target_dsn)
    try:
        async with source_pool.acquire() as connection:
            await _ensure_pointer(connection, state)
            active = await _read_pointer(connection)
        if active["active_identity"] != state["target_identity"]:
            raise _contract_error("SECRETLESS_SWITCH_TARGET_NOT_ACTIVE", field="active_identity")
        if inject_http_status:
            if inject_http_status != 503:
                raise _contract_error("SECRETLESS_SWITCH_UNSUPPORTED_INJECTED_STATUS", field="inject_http_status")
            return {
                "schema_version": "secretless-switch-contract/v1",
                "status": "expected_failure",
                "action": "probe",
                "head_sha": state["head_sha"],
                "operation_id": state["operation_id"],
                "active_identity": active["active_identity"],
                "http_status": 503,
                "error_code": "SWITCH_PROBE_HTTP_503",
                "rollback_required": True,
            }
        business_probe = await _business_snapshot(target_pool)
        return {
            "schema_version": "secretless-switch-contract/v1",
            "status": "success",
            "action": "probe",
            "head_sha": state["head_sha"],
            "operation_id": state["operation_id"],
            "active_identity": active["active_identity"],
            "http_status": 200,
            "business_probe": business_probe,
        }
    finally:
        await target_pool.close()
        await source_pool.close()


async def _rollback(state: dict[str, str], source_dsn: str) -> dict[str, Any]:
    _validate_state(state, source_dsn)
    source_pool = await asyncpg.create_pool(
        source_dsn,
        min_size=0,
        max_size=1,
        **_CRDB_CONNECTION_OPTIONS,
    )
    try:
        async with source_pool.acquire() as connection, connection.transaction():
            await _ensure_pointer(connection, state)
            before = await _read_pointer(connection)
            if (
                before["active_database"] != state["target_database"]
                or before["active_identity"] != state["target_identity"]
                or before["head_sha"] != state["head_sha"]
                or before["operation_id"] != state["operation_id"]
            ):
                raise _contract_error("SECRETLESS_ROLLBACK_POINTER_TARGET_STATE_INVALID", field="active_pointer")
            after = await _cas_pointer(
                connection,
                expected=before,
                database=state["source_database"],
                identity=state["source_identity"],
                state=state,
            )
        if after["active_identity"] != state["source_identity"]:
            raise _contract_error("SECRETLESS_ROLLBACK_SOURCE_NOT_ACTIVE", field="active_identity")
        source_probe = await _business_snapshot(source_pool)
        return {
            "schema_version": "secretless-switch-contract/v1",
            "status": "success",
            "action": "rollback",
            "head_sha": state["head_sha"],
            "operation_id": state["operation_id"],
            "source_identity": state["source_identity"],
            "target_identity": state["target_identity"],
            "active_before": before,
            "active_after": after,
            "source_business_probe": source_probe,
        }
    finally:
        await source_pool.close()


async def _drop_target(state: dict[str, str], source_dsn: str) -> dict[str, Any]:
    _validate_state(state, source_dsn)
    connection = await asyncpg.connect(
        source_dsn,
        **_CRDB_CONNECTION_OPTIONS,
    )
    try:
        await _ensure_pointer(connection, state)
        active = await _read_pointer(connection)
        if active["active_identity"] != state["source_identity"]:
            raise _contract_error("SECRETLESS_SWITCH_DROP_TARGET_WHILE_ACTIVE", field="active_identity")
        await connection.execute(
            f"DROP DATABASE IF EXISTS {_quote_identifier(state['target_database'])} CASCADE"
        )
        existence_rows = await connection.fetch(
            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_database WHERE datname=$1) "
            "AS target_exists",
            state["target_database"],
        )
        if len(existence_rows) != 1:
            raise _contract_error(
                "SECRETLESS_SWITCH_TARGET_DROP_CHECK_INVALID",
                field="target_database",
            )
        exists = bool(existence_rows[0]["target_exists"])
        if exists:
            raise _contract_error("SECRETLESS_SWITCH_TARGET_DROP_FAILED", field="target_database")
        return {
            "schema_version": "secretless-switch-contract/v1",
            "status": "success",
            "action": "drop-target",
            "head_sha": state["head_sha"],
            "operation_id": state["operation_id"],
            "source_identity": state["source_identity"],
            "target_identity": state["target_identity"],
            "target_database": state["target_database"],
            "target_exists_after": False,
        }
    finally:
        await connection.close()


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, AppError):
        reason = str(exc.params.get("reason", "")).strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_.-]{2,159}", reason):
            return reason
        return exc.code
    message = str(exc).strip()
    candidate = message.split(":", 1)[0].strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_.-]{2,159}", candidate):
        return candidate
    return ErrorCodes.SECRETLESS_SWITCH_INTERNAL_UNEXPECTED


def _write_json(path: Path | None, document: dict[str, Any]) -> None:
    if path is None:
        print(json.dumps(document, ensure_ascii=False, indent=2))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("switch", "probe", "rollback", "drop-target"))
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--source-dsn", default="")
    parser.add_argument("--inject-http-status", type=int, default=0)
    parser.add_argument("--output-json", type=Path)
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    _require_secretless_boundary()
    state = _load_state(args.state_file)
    source_dsn = args.source_dsn or os.environ.get("SECRETLESS_CRDB_URL", "")
    if not source_dsn:
        raise _contract_error("SECRETLESS_CRDB_URL_MISSING", field="SECRETLESS_CRDB_URL")
    if args.command == "switch":
        return await _switch(state, source_dsn)
    if args.command == "probe":
        return await _probe(state, source_dsn, args.inject_http_status)
    if args.command == "rollback":
        return await _rollback(state, source_dsn)
    return await _drop_target(state, source_dsn)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        document = asyncio.run(_run(args))
        returncode = 0 if document.get("status") in {"success", "expected_failure"} else 1
    except (OSError, RuntimeError, TypeError, ValueError, AppError, asyncpg.PostgresError) as exc:
        document = {
            "schema_version": "secretless-switch-contract-error/v1",
            "status": "failure",
            "error_code": _error_code(exc),
            "error_message": f"{type(exc).__name__}: {exc}",
        }
        returncode = 1
    _write_json(args.output_json, document)
    return returncode


if __name__ == "__main__":
    sys.exit(main())
