"""P0-2 / P0-3 回归：数据库备份恢复相关安全性。

- P0-2：restore_from_backup 必须对表名 / 列名做格式白名单校验，恶意标识符不得进入 SQL。
- P0-3：_redact_secrets 的脱敏集 (_SENSITIVE_FIELDS) 已置空（脱敏禁用），
        备份中的敏感字段（api_hash / r2_secret_key / r2_access_key）应原样保留，
        否则恢复后凭证变为占位符、废库。

被测函数：services/db_backup.py :: restore_from_backup / _redact_secrets / _SENSITIVE_FIELDS
"""

import asyncio
import json
from unittest.mock import MagicMock, AsyncMock

import services.db_backup as db_backup


def test_restore_rejects_malicious_column_name(monkeypatch):
    # 1) DB 客户端 mock（不触发真实连接）
    fake_client = MagicMock()
    fake_client.is_connected = True
    fake_client.execute = AsyncMock()
    monkeypatch.setattr(db_backup, "db_client", fake_client)

    # 2) R2 存储 mock（download 返回构造的恶意备份）
    fake_r2 = MagicMock()
    # 注意：restore_from_backup 对 configure 的调用不带 await（见 services/db_backup.py:245），
    # 故 configure 必须是同步 MagicMock；connect/download 带 await，用 AsyncMock。
    fake_r2.configure = MagicMock()
    fake_r2.connect = AsyncMock()
    fake_r2.download = AsyncMock()
    monkeypatch.setattr(db_backup, "r2_storage", fake_r2)

    malicious_table = "users; DROP TABLE users;--"
    malicious_col = "notes; DROP TABLE users;--"
    backup = {
        "tables": {
            malicious_table: [{"user_id": 1, "name": "x"}],
            "users": [
                {"user_id": 1, "name": "alice"},
                {malicious_col: "x", "user_id": 2},
            ],
        }
    }
    fake_r2.download.return_value = json.dumps(backup)

    async def _run():
        return await db_backup.restore_from_backup("db_backup/test.json")

    result = asyncio.run(_run())

    # 任何执行的 SQL 都不得包含注入片段
    all_sql = " ".join(str(c.args[0]) for c in fake_client.execute.call_args_list)
    assert "DROP TABLE" not in all_sql
    assert malicious_table not in all_sql
    assert malicious_col not in all_sql

    # 恶意表名应被跳过并记录 warn
    assert malicious_table in result["skipped"]
    assert db_backup.logger.warning.called

    # 合法行（alice）仍应被正常 INSERT
    inserted = [
        c for c in fake_client.execute.call_args_list
        if str(c.args[0]).startswith('INSERT INTO "users"')
    ]
    assert inserted, "合法 users 行应被 INSERT"
    assert "alice" in str(inserted[0].args)


def test_backup_restore_preserves_secrets():
    # P0-3：脱敏集为空（已禁用脱敏）
    assert db_backup._SENSITIVE_FIELDS == set()

    data = {
        "tables": {
            "backup_config": [
                {"config_key": "r2_secret_key", "config_value": "SUPER_SECRET"},
                {"config_key": "r2_access_key", "config_value": "AKIA123"},
            ],
            "relay_accounts": [
                {"phone": "+100", "api_hash": "REAL_API_HASH", "api_id": 123},
            ],
        }
    }
    result = db_backup._redact_secrets(data)

    # 返回同一对象且敏感值未变（恢复后凭证仍可用）
    assert result is data
    cfg = {
        row["config_key"]: row["config_value"]
        for row in result["tables"]["backup_config"]
    }
    assert cfg["r2_secret_key"] == "SUPER_SECRET"
    assert cfg["r2_access_key"] == "AKIA123"
    relay = result["tables"]["relay_accounts"][0]
    assert relay["api_hash"] == "REAL_API_HASH"
