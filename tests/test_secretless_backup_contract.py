"""R83 Secretless exact backup contract 与隔离 CRDB restore 单元测试。"""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timezone
from decimal import Decimal
from types import MappingProxyType
from urllib.parse import urlsplit

import pytest

from services import secretless_backup_contract as mod
from services.backup_dr_validate import ExactBackupContract
from services.error_codes import AppError, ErrorCodes


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self, database: str, state: dict):
        self.database = database
        self.state = state

    def transaction(self):
        return _Transaction()

    async def execute(self, sql: str, *values):
        self.state.setdefault("executed", []).append((self.database, sql, values))
        if sql.startswith("CREATE DATABASE"):
            database = sql.split('"')[1]
            self.state["databases"].add(database)
        elif sql.startswith("DROP DATABASE"):
            database = sql.split('"')[1]
            self.state["databases"].discard(database)
        return "OK"

    async def fetch(self, sql: str):
        self.state.setdefault("fetches", []).append((self.database, sql))
        if "udt_name" in sql:
            return self.state.get("column_type_records", [])
        return []

    async def fetchval(self, sql: str):
        if "information_schema.tables" in sql:
            return self.state.get("target_table_count", 0)
        if "SELECT count(*) FROM" in sql:
            return self.state.get("business_probe", 0)
        return 0

    async def close(self):
        return None


class _Pool:
    def __init__(self, database: str, state: dict):
        self.connection = _Connection(database, state)
        self.closed = False

    @asynccontextmanager
    async def acquire(self):
        yield self.connection

    async def close(self):
        self.closed = True


def _contract(*, plaintext: bytes, source_identity: str) -> ExactBackupContract:
    manifest = MappingProxyType({
        "table_stats": MappingProxyType({
            "users": MappingProxyType({"row_count": 1, "source": "crdb"}),
        }),
    })
    return ExactBackupContract(
        valid=True,
        backup_id="backup-id",
        payload_key="payload.enc",
        manifest_key="manifest.json",
        complete_key="COMPLETE",
        manifest_sha256="1" * 64,
        ciphertext_sha256="2" * 64,
        plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
        schema_version=mod._SCHEMA_VERSION,
        source_sha="a" * 40,
        source_database_identity=source_identity,
        schema_fingerprint=mod._schema_fingerprint(),
        manifest=manifest,
        plaintext_bytes=plaintext,
    )


def _args(source_dsn: str) -> argparse.Namespace:
    return argparse.Namespace(
        backup_id="backup-id",
        payload_key="payload.enc",
        manifest_key="manifest.json",
        complete_key="COMPLETE",
        current_schema_version=mod._SCHEMA_VERSION,
        source_dsn=source_dsn,
        target_database="tgjiema_restore_test_target",
        operation_id="operation-id",
    )


def test_secretless_boundary_requires_test_and_flag(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRETLESS_MODE", "true")
    with pytest.raises(AppError) as exc_info:
        mod._require_secretless_boundary()
    assert exc_info.value.params["reason"] == "SECRETLESS_RESTORE_APP_ENV_INVALID"

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("SECRETLESS_MODE", raising=False)
    with pytest.raises(AppError) as exc_info:
        mod._require_secretless_boundary()
    assert exc_info.value.params["reason"] == "SECRETLESS_RESTORE_MODE_INVALID"


def test_identifier_and_target_dsn_are_fail_closed():
    with pytest.raises(AppError) as exc_info:
        mod._safe_identifier("source; DROP DATABASE source")
    assert exc_info.value.params["reason"] == "SECRETLESS_RESTORE_DATABASE_IDENTIFIER_INVALID"
    with pytest.raises(AppError) as exc_info:
        mod._dsn_with_database("sqlite:///tmp/source.db", "target")
    assert exc_info.value.params["reason"] == "SECRETLESS_RESTORE_SOURCE_DSN_INVALID"
    target = mod._dsn_with_database(
        "postgresql://root@cockroachdb:26258/source?sslmode=disable", "target"
    )
    assert urlsplit(target).path == "/target"


def test_error_code_rejects_arbitrary_exception_text():
    assert mod._error_code(RuntimeError("RESTORE_TARGET_NOT_BLANK")) == (
        "RESTORE_TARGET_NOT_BLANK"
    )
    assert (
        mod._error_code(ValueError("some arbitrary text"))
        == ErrorCodes.SECRETLESS_INTERNAL_UNEXPECTED
    )


def test_contract_document_recursively_serializes_frozen_manifest():
    plaintext = b'{"tables":{"users":[{"user_id":1}]}}'
    contract = _contract(plaintext=plaintext, source_identity="1" * 16)

    document = mod._contract_document(contract)
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True)

    assert document["table_stats"] == {
        "users": {"row_count": 1, "source": "crdb"}
    }
    assert "mappingproxy" not in encoded


def test_json_value_rejects_unknown_evidence_types():
    with pytest.raises(TypeError, match="unsupported contract evidence value"):
        mod._json_value(object())


def test_restore_value_coercion_covers_crdb_types():
    object_id = uuid.uuid4()
    assert mod._coerce_restore_value({"b": 2, "a": [1]}, "jsonb") == (
        '{"a":[1],"b":2}'
    )
    assert mod._coerce_restore_value('[{"a":1}]', "jsonb") == '[{"a":1}]'
    assert mod._coerce_restore_value("true", "bool") is True
    assert mod._coerce_restore_value(0, "boolean") is False
    assert mod._coerce_restore_value("12.340", "numeric") == Decimal("12.340")
    assert mod._coerce_restore_value(str(object_id), "uuid") == object_id
    assert mod._coerce_restore_value("2026-07-29T01:02:03Z", "timestamptz") == (
        datetime(2026, 7, 29, 1, 2, 3, tzinfo=timezone.utc)
    )
    assert mod._coerce_restore_value("2026-07-29", "date") == date(2026, 7, 29)
    assert mod._coerce_restore_value("01:02:03", "time") == time(1, 2, 3)


@pytest.mark.parametrize("value", ["truthy", 2, [], {}])
def test_restore_value_coercion_rejects_invalid_booleans(value):
    with pytest.raises(ValueError, match="invalid boolean restore value"):
        mod._coerce_restore_value(value, "bool")


@pytest.mark.asyncio
async def test_restore_payload_uses_target_column_types():
    state = {
        "databases": {"target"},
        "column_type_records": [
            {
                "table_name": "file_records",
                "column_name": "file_code",
                "data_type": "text",
                "udt_name": "text",
            },
            {
                "table_name": "file_records",
                "column_name": "blocked_users",
                "data_type": "jsonb",
                "udt_name": "jsonb",
            },
            {
                "table_name": "file_records",
                "column_name": "protect_content",
                "data_type": "boolean",
                "udt_name": "bool",
            },
        ],
    }
    pool = _Pool("target", state)

    restored = await mod._restore_crdb_payload(
        pool,
        {
            "file_records": [
                {
                    "file_code": "ABC",
                    "blocked_users": [3, 1],
                    "protect_content": "true",
                }
            ]
        },
    )

    assert restored == {"file_records": 1}
    insert = next(
        item for item in state["executed"] if item[1].startswith("INSERT INTO")
    )
    assert insert[2] == ("ABC", "[3,1]", True)


@pytest.mark.asyncio
async def test_restore_uses_independent_target_and_preserves_source(monkeypatch):
    source_dsn = "postgresql://root@cockroachdb:26258/source?sslmode=disable"
    plaintext = json.dumps({
        "tables": {
            "users": [{"user_id": 1}],
            "manifest": [{"group_id": "sqlite-only"}],
        }
    }).encode()
    contract = _contract(
        plaintext=plaintext,
        source_identity=mod._database_identity(source_dsn),
    )
    state = {"databases": {"source"}, "target_table_count": 0, "business_probe": 1}
    pools: list[tuple[str, _Pool]] = []

    async def fake_validate(_args):
        return contract

    async def fake_create_pool(dsn: str, **_kwargs):
        database = urlsplit(dsn).path.lstrip("/")
        pool = _Pool(database, state)
        pools.append((database, pool))
        return pool

    async def fake_capture(_pool, tables):
        return {
            table: {
                "row_count": len(rows),
                "field_hash": mod._rows_digest(rows, mod.BACKUP_SCHEMA[table].pk_columns),
                "columns": list(rows[0]) if rows else [],
            }
            for table, rows in tables.items()
        }

    async def fake_initialize(_pool):
        return None

    async def fake_schema(_pool):
        return {"users": ["user_id"]}

    async def fake_restore(_pool, tables):
        assert set(tables) == {"users"}
        return {"users": 1}

    monkeypatch.setattr(mod, "_validate", fake_validate)
    monkeypatch.setattr(mod.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(mod, "_capture_table_snapshot", fake_capture)
    monkeypatch.setattr(mod, "_initialize_target", fake_initialize)
    monkeypatch.setattr(mod, "_target_schema_snapshot", fake_schema)
    monkeypatch.setattr(mod, "_restore_crdb_payload", fake_restore)

    result = await mod._restore(_args(source_dsn))

    assert result["status"] == "success"
    assert result["source_identity"] != result["target_identity"]
    assert result["target_before"] == {"user_table_count": 0, "blank": True}
    assert result["source_unchanged"] is True
    assert result["target_after"] == result["payload_snapshot"]
    assert result["business_probe"] == {"status": "pass", "row_count": 1}
    assert [database for database, _pool in pools] == [
        "source",
        "tgjiema_restore_test_target",
    ]
    assert "tgjiema_restore_test_target" in state["databases"]
    assert all(pool.closed for _database, pool in pools)


@pytest.mark.asyncio
async def test_non_blank_target_fails_and_drops_only_run_target(monkeypatch):
    source_dsn = "postgresql://root@cockroachdb:26258/source?sslmode=disable"
    plaintext = json.dumps({"tables": {"users": [{"user_id": 1}]}}).encode()
    contract = _contract(
        plaintext=plaintext,
        source_identity=mod._database_identity(source_dsn),
    )
    state = {"databases": {"source", "other_database"}, "target_table_count": 1}

    async def fake_validate(_args):
        return contract

    async def fake_create_pool(dsn: str, **_kwargs):
        return _Pool(urlsplit(dsn).path.lstrip("/"), state)

    async def fake_capture(_pool, tables):
        return {
            table: {
                "row_count": len(rows),
                "field_hash": mod._rows_digest(rows, mod.BACKUP_SCHEMA[table].pk_columns),
                "columns": list(rows[0]),
            }
            for table, rows in tables.items()
        }

    async def fake_drop_target(_source_dsn, target_database):
        state["databases"].discard(target_database)

    monkeypatch.setattr(mod, "_validate", fake_validate)
    monkeypatch.setattr(mod.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(mod, "_capture_table_snapshot", fake_capture)
    monkeypatch.setattr(mod, "_drop_target", fake_drop_target)

    with pytest.raises(AppError) as exc_info:
        await mod._restore(_args(source_dsn))
    assert exc_info.value.params["reason"] == "RESTORE_TARGET_NOT_BLANK"

    assert state["databases"] == {"source", "other_database"}
