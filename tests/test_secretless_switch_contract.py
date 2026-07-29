"""R83 Step 13 Secretless CRDB identity switch/rollback contract tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import services.secretless_switch_contract as contract
from services.error_codes import AppError

_SOURCE_DSN = "postgresql://root@cockroachdb:26258/tgjiema?sslmode=disable"
_TARGET_DB = "tgjiema_restore_run_one"
_TARGET_DSN = contract._dsn_with_database(_SOURCE_DSN, _TARGET_DB)


def _state() -> dict[str, str]:
    return {
        "schema_version": "secretless-restore-state/v1",
        "head_sha": "a" * 40,
        "backup_id": "backup-one",
        "operation_id": "operation-one",
        "source_identity": contract._database_identity(_SOURCE_DSN),
        "target_identity": contract._database_identity(_TARGET_DSN),
        "source_database": "tgjiema",
        "target_database": _TARGET_DB,
        "target_dsn_sha256": hashlib.sha256(_TARGET_DSN.encode()).hexdigest(),
    }


def test_load_and_validate_current_sha_bound_state(tmp_path: Path, monkeypatch):
    state_path = tmp_path / "restore-state.json"
    state_path.write_text(json.dumps(_state()), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)

    loaded = contract._load_state(state_path)
    source_database, target_dsn = contract._validate_state(loaded, _SOURCE_DSN)

    assert source_database == "tgjiema"
    assert target_dsn == _TARGET_DSN
    assert loaded["source_identity"] != loaded["target_identity"]


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("head_sha", "b" * 40, "SECRETLESS_SWITCH_HEAD_SHA_MISMATCH"),
        ("source_identity", "wrong", "SECRETLESS_SWITCH_SOURCE_IDENTITY_MISMATCH"),
        ("target_identity", "wrong", "SECRETLESS_SWITCH_TARGET_IDENTITY_MISMATCH"),
        ("target_dsn_sha256", "0" * 64, "SECRETLESS_SWITCH_TARGET_DSN_DIGEST_MISMATCH"),
    ],
)
def test_state_identity_drift_is_fail_closed(monkeypatch, field, value, error_code):
    state = _state()
    state[field] = value
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)

    with pytest.raises(AppError) as exc_info:
        contract._validate_state(state, _SOURCE_DSN)
    assert exc_info.value.params["reason"] == error_code


def test_identifier_injection_is_rejected():
    with pytest.raises(AppError) as exc_info:
        contract._dsn_with_database(_SOURCE_DSN, 'tgjiema"; DROP DATABASE tgjiema')
    assert exc_info.value.params["reason"] == "SECRETLESS_SWITCH_DATABASE_IDENTIFIER_INVALID"


class _Connection:
    def __init__(self):
        self.pointer = {
            "active_database": "tgjiema",
            "active_identity": "source-id",
            "version": 3,
            "head_sha": "a" * 40,
            "operation_id": "operation-one",
        }

    async def execute(self, query, *args):
        if query.startswith("UPDATE"):
            if (
                args[4] != self.pointer["version"]
                or args[5] != self.pointer["active_database"]
                or args[6] != self.pointer["active_identity"]
                or args[7] != self.pointer["head_sha"]
                or args[8] != self.pointer["operation_id"]
            ):
                return "UPDATE 0"
            self.pointer.update({
                "active_database": args[0],
                "active_identity": args[1],
                "version": self.pointer["version"] + 1,
                "head_sha": args[2],
                "operation_id": args[3],
            })
            return "UPDATE 1"
        return "CREATE TABLE"

    async def fetch(self, _query):
        return [dict(self.pointer)]


@pytest.mark.asyncio
async def test_pointer_compare_and_swap_binds_version_identity_sha_and_operation():
    connection = _Connection()
    before = dict(connection.pointer)
    state = _state()
    state["source_identity"] = "source-id"
    state["target_identity"] = "target-id"

    after = await contract._cas_pointer(
        connection,
        expected=before,
        database=_TARGET_DB,
        identity="target-id",
        state=state,
    )

    assert after["active_database"] == _TARGET_DB
    assert after["active_identity"] == "target-id"
    assert after["version"] == 4
    assert after["head_sha"] == state["head_sha"]
    assert after["operation_id"] == state["operation_id"]


@pytest.mark.asyncio
async def test_pointer_compare_and_swap_rejects_stale_version():
    connection = _Connection()
    stale = dict(connection.pointer)
    stale["version"] = 2
    state = _state()

    with pytest.raises(AppError) as exc_info:
        await contract._cas_pointer(
            connection,
            expected=stale,
            database=_TARGET_DB,
            identity=state["target_identity"],
            state=state,
        )
    assert exc_info.value.params["reason"] == "SECRETLESS_SWITCH_POINTER_CAS_FAILED"


@pytest.mark.asyncio
async def test_pointer_read_fully_consumes_result_set():
    connection = _Connection()

    pointer = await contract._read_pointer(connection)

    assert pointer == connection.pointer
    assert not hasattr(connection, "fetchrow")
    assert not hasattr(connection, "fetchval")


@pytest.mark.parametrize("app_env,mode", [("production", "true"), ("test", "false")])
def test_switch_boundary_rejects_non_secretless(monkeypatch, app_env, mode):
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("SECRETLESS_MODE", mode)
    with pytest.raises(AppError):
        contract._require_secretless_boundary()


class _ClosablePool:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_switch_pools_disable_asyncpg_statement_cache(monkeypatch):
    calls = []

    async def fake_create_pool(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return _ClosablePool(dsn)

    monkeypatch.setattr(contract.asyncpg, "create_pool", fake_create_pool)

    source_pool, target_pool = await contract._connect_pools(
        _SOURCE_DSN,
        _TARGET_DSN,
    )
    await target_pool.close()
    await source_pool.close()

    assert [dsn for dsn, _kwargs in calls] == [_SOURCE_DSN, _TARGET_DSN]
    for _dsn, kwargs in calls:
        assert kwargs["statement_cache_size"] == 0
        assert kwargs["command_timeout"] == 30
        assert kwargs["min_size"] == 0
        assert kwargs["max_size"] == 1


@pytest.mark.asyncio
async def test_rollback_pool_disables_asyncpg_statement_cache(monkeypatch):
    captured = {}

    async def fake_create_pool(dsn, **kwargs):
        captured.update({"dsn": dsn, **kwargs})
        raise RuntimeError("stop-after-connect-options")

    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(contract.asyncpg, "create_pool", fake_create_pool)

    with pytest.raises(RuntimeError, match="stop-after-connect-options"):
        await contract._rollback(_state(), _SOURCE_DSN)

    assert captured["dsn"] == _SOURCE_DSN
    assert captured["statement_cache_size"] == 0
    assert captured["command_timeout"] == 30


@pytest.mark.asyncio
async def test_drop_target_connection_disables_asyncpg_statement_cache(monkeypatch):
    captured = {}

    async def fake_connect(dsn, **kwargs):
        captured.update({"dsn": dsn, **kwargs})
        raise RuntimeError("stop-after-connect-options")

    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(contract.asyncpg, "connect", fake_connect)

    with pytest.raises(RuntimeError, match="stop-after-connect-options"):
        await contract._drop_target(_state(), _SOURCE_DSN)

    assert captured["dsn"] == _SOURCE_DSN
    assert captured["statement_cache_size"] == 0
    assert captured["command_timeout"] == 30
