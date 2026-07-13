"""R35 Batch 7: P2 工程与安全改进测试。

被测目标:
- ``utils.security.hash_api_credential`` — 凭证指纹化(SHA-256 前 8 位)
- ``config.settings.Settings`` — ALLOWED_DECODER_BOTS_FAIL_CLOSED 配置
- ``database.cache_store.CacheStore.get_manifest_by_file_unique_id`` — 精确索引查询
- ``services.delivery_resolver.ReplicaAwareResolver`` — fail_closed 语义
  - resolve_channel_for_file(fail_closed=True/False)
  - get_available_replicas(fail_closed=True/False) 使用精确索引
  - verify_replica_exists(fail_closed=True/False) 不再乐观放行
- ``services.relay_instance.RelayInstance.start`` — api_hash 日志指纹化
- ``services.relay_instance.RelayInstance.send_external_code`` — 白名单 fail-closed

测试策略:
- 单元测试使用 MagicMock / AsyncMock 模拟 CacheStore
- 不依赖真实数据库或 Telethon
- 验证 fail-closed / fail-open 行为分支
- 验证凭证指纹化(原值不出现在日志中)

对应 R35 P2 改进项:
- P2-1: ALLOWED_DECODER_BOTS fail-closed
- P2-2: api_hash 日志指纹化
- P2-3: ReplicaAwareResolver fail-closed
- P2-4: ReplicaAwareResolver 精确索引查询
"""
import inspect
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── 模块可用性检查 ────────────────────────────────────────────

_security_available = True
try:
    from utils.security import hash_api_credential
except Exception:
    _security_available = False

_resolver_available = True
try:
    from services.delivery_resolver import ReplicaAwareResolver
except Exception:
    _resolver_available = False

_settings_available = True
try:
    from config.settings import Settings
except Exception:
    _settings_available = False

_cache_store_available = True
try:
    from database.cache_store import CacheStore as _CacheStoreCls
    if not inspect.isclass(_CacheStoreCls):
        _cache_store_available = False
except Exception:
    _cache_store_available = False


# ════════════════════════════════════════════════════════════════
# 1. hash_api_credential 凭证指纹化测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _security_available, reason="utils.security 不可用")
class TestHashApiCredential:
    """P2-2: 凭证指纹化测试。"""

    def test_returns_8_char_hex(self):
        """指纹应为 8 位十六进制。"""
        fp = hash_api_credential("abcdef0123456789abcdef0123456789")
        assert len(fp) == 8
        # 全部为十六进制字符
        assert all(c in "0123456789abcdef" for c in fp)

    def test_same_input_same_fingerprint(self):
        """相同原值必返回相同指纹(便于日志关联)。"""
        v = "my-secret-api-hash-12345"
        assert hash_api_credential(v) == hash_api_credential(v)

    def test_different_input_different_fingerprint(self):
        """不同原值应返回不同指纹(理论上碰撞概率极低)。"""
        fp1 = hash_api_credential("hash-1")
        fp2 = hash_api_credential("hash-2")
        assert fp1 != fp2

    def test_empty_returns_empty_marker(self):
        """空值返回 'empty' 标识。"""
        assert hash_api_credential("") == "empty"
        assert hash_api_credential(None) == "empty"

    def test_fingerprint_is_irreversible(self):
        """指纹不可逆:不包含原值的任何子串(原值足够长时)。"""
        long_value = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
        fp = hash_api_credential(long_value)
        # 原值的任何 4+ 字符子串都不应出现在指纹中
        for i in range(len(long_value) - 3):
            assert long_value[i:i + 4] not in fp, (
                f"原值子串 '{long_value[i:i+4]}' 出现在指纹中,违反不可逆性"
            )


# ════════════════════════════════════════════════════════════════
# 2. ALLOWED_DECODER_BOTS_FAIL_CLOSED 配置测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _settings_available, reason="config.settings 不可用")
class TestAllowedDecoderBotsFailClosed:
    """P2-1: ALLOWED_DECODER_BOTS_FAIL_CLOSED 配置测试。"""

    def test_default_is_false(self):
        """默认值应为 False(保持向后兼容)。"""
        s = Settings()
        assert s.ALLOWED_DECODER_BOTS_FAIL_CLOSED is False
        assert s.ALLOWED_DECODER_BOTS == ""

    def test_can_be_set_to_true(self):
        """可被设为 True(商用部署)。"""
        s = Settings()
        s.ALLOWED_DECODER_BOTS_FAIL_CLOSED = True
        assert s.ALLOWED_DECODER_BOTS_FAIL_CLOSED is True


# ════════════════════════════════════════════════════════════════
# 3. ReplicaAwareResolver fail_closed 测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _resolver_available, reason="services.delivery_resolver 不可用")
class TestReplicaAwareResolverFailClosed:
    """P2-3: ReplicaAwareResolver fail_closed 行为测试。"""

    @pytest.mark.asyncio
    async def test_get_available_replicas_uses_precise_index(self):
        """P2-4: get_available_replicas 应调用 get_manifest_by_file_unique_id 精确索引。"""
        fake_store = MagicMock()
        fake_store.get_manifest_by_file_unique_id = AsyncMock(
            return_value=[
                {
                    "group_id": 1, "file_unique_id": "fuid-1",
                    "channel_id": 100, "message_id": 500,
                    "media_type": "photo", "media_group_id": "", "first_seen_at": "",
                },
                {
                    "group_id": 1, "file_unique_id": "fuid-1",
                    "channel_id": 200, "message_id": 600,
                    "media_type": "photo", "media_group_id": "", "first_seen_at": "",
                },
            ]
        )
        # cells_local 返回空(避免 _rank_by_status_and_health 异常)
        fake_store.get_all_cells_local = AsyncMock(return_value=[])
        fake_store.load_cells_snapshot = AsyncMock(return_value=([], ""))

        resolver = ReplicaAwareResolver(fake_store)
        replicas = await resolver.get_available_replicas("fuid-1", 1)

        # 验证调用的是精确索引方法
        fake_store.get_manifest_by_file_unique_id.assert_awaited_once_with("fuid-1", 1)
        # 不应调用 get_manifest_by_group(旧的全组扫描)
        fake_store.get_manifest_by_group.assert_not_called()
        assert len(replicas) == 2
        assert {r["channel_id"] for r in replicas} == {100, 200}

    @pytest.mark.asyncio
    async def test_get_available_replicas_query_exception_returns_empty(self):
        """查询异常时返回空列表(无论 fail_closed,均不抛异常)。"""
        fake_store = MagicMock()
        fake_store.get_manifest_by_file_unique_id = AsyncMock(
            side_effect=RuntimeError("db connection lost")
        )

        resolver = ReplicaAwareResolver(fake_store)
        # fail_closed=True
        result = await resolver.get_available_replicas("fuid-1", 1, fail_closed=True)
        assert result == []
        # fail_closed=False 也返回空列表
        result2 = await resolver.get_available_replicas("fuid-1", 1, fail_closed=False)
        assert result2 == []

    @pytest.mark.asyncio
    async def test_resolve_channel_for_file_default_fail_closed(self):
        """resolve_channel_for_file 默认 fail_closed=True。"""
        fake_store = MagicMock()
        fake_store.get_manifest_by_file_unique_id = AsyncMock(return_value=[])
        fake_store.get_all_cells_local = AsyncMock(return_value=[])
        fake_store.load_cells_snapshot = AsyncMock(return_value=([], ""))

        resolver = ReplicaAwareResolver(fake_store)
        # 无副本时返回 None
        result = await resolver.resolve_channel_for_file("fuid-1", 1)
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_channel_for_file_with_replicas(self):
        """有副本时返回 (channel_id, message_id)。"""
        fake_store = MagicMock()
        fake_store.get_manifest_by_file_unique_id = AsyncMock(
            return_value=[
                {"group_id": 1, "file_unique_id": "fuid-1",
                 "channel_id": 100, "message_id": 500,
                 "media_type": "photo", "media_group_id": "", "first_seen_at": ""},
            ]
        )
        fake_store.get_all_cells_local = AsyncMock(return_value=[])
        fake_store.load_cells_snapshot = AsyncMock(return_value=([], ""))

        resolver = ReplicaAwareResolver(fake_store)
        result = await resolver.resolve_channel_for_file("fuid-1", 1)
        assert result == (100, 500)

    @pytest.mark.asyncio
    async def test_verify_replica_exists_no_context_fail_closed(self):
        """verify 无 group 上下文时 fail_closed=True 返回 False(拒绝)。"""
        fake_store = MagicMock()
        resolver = ReplicaAwareResolver(fake_store)
        # 不设置 _context_group_id(默认 None)
        assert resolver._context_group_id is None
        result = await resolver.verify_replica_exists(100, 500, fail_closed=True)
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_replica_exists_no_context_fail_open(self):
        """verify 无 group 上下文时 fail_closed=False 乐观放行(兼容)。"""
        fake_store = MagicMock()
        resolver = ReplicaAwareResolver(fake_store)
        result = await resolver.verify_replica_exists(100, 500, fail_closed=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_replica_exists_query_exception_fail_closed(self):
        """verify 查询异常时 fail_closed=True 返回 False。"""
        fake_store = MagicMock()
        fake_store.get_manifest_by_group = AsyncMock(
            side_effect=RuntimeError("db error")
        )
        resolver = ReplicaAwareResolver(fake_store)
        resolver._context_group_id = 1  # 设置上下文以触发查询
        result = await resolver.verify_replica_exists(100, 500, fail_closed=True)
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_replica_exists_query_exception_fail_open(self):
        """verify 查询异常时 fail_closed=False 乐观放行(兼容)。"""
        fake_store = MagicMock()
        fake_store.get_manifest_by_group = AsyncMock(
            side_effect=RuntimeError("db error")
        )
        resolver = ReplicaAwareResolver(fake_store)
        resolver._context_group_id = 1
        result = await resolver.verify_replica_exists(100, 500, fail_closed=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_replica_exists_uses_cache(self):
        """verify 优先使用缓存(零查询)。"""
        fake_store = MagicMock()
        fake_store.get_manifest_by_group = AsyncMock(return_value=[])
        resolver = ReplicaAwareResolver(fake_store)
        # 设置缓存命中
        resolver._last_replicas = [
            {"channel_id": 100, "message_id": 500, "media_type": "", "media_group_id": ""}
        ]
        result = await resolver.verify_replica_exists(100, 500, fail_closed=True)
        assert result is True
        # 不应触发数据库查询
        fake_store.get_manifest_by_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_channel_for_file_fail_closed_propagates(self):
        """resolve_channel_for_file 应将 fail_closed 传递给 get_available_replicas。"""
        fake_store = MagicMock()
        fake_store.get_manifest_by_file_unique_id = AsyncMock(return_value=[])

        resolver = ReplicaAwareResolver(fake_store)
        with patch.object(
            resolver, "get_available_replicas", new=AsyncMock(return_value=[])
        ) as mock_get:
            await resolver.resolve_channel_for_file(
                "fuid-1", 1, fail_closed=False
            )
            # 验证 fail_closed 被传递
            call_kwargs = mock_get.await_args.kwargs
            assert call_kwargs.get("fail_closed") is False


# ════════════════════════════════════════════════════════════════
# 4. CacheStore.get_manifest_by_file_unique_id 精确索引测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _cache_store_available, reason="database.cache_store 不可用")
class TestGetManifestByFileUniqueId:
    """P2-4: 精确索引查询测试。"""

    @pytest.mark.asyncio
    async def test_empty_file_unique_id_returns_empty(self, tmp_path):
        """空 file_unique_id 返回空列表。"""
        from database.cache_store import CacheStore
        store = CacheStore(str(tmp_path / "test.db"))
        try:
            await store.init()
            result = await store.get_manifest_by_file_unique_id("", group_id=1)
            assert result == []
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_no_db_returns_empty(self, tmp_path):
        """_db 为 None 时返回空列表。"""
        from database.cache_store import CacheStore
        store = CacheStore(str(tmp_path / "test.db"))
        # 不调用 init(),_db 仍为 None
        result = await store.get_manifest_by_file_unique_id("fuid-1", group_id=1)
        assert result == []

    @pytest.mark.asyncio
    async def test_query_with_group_id(self, tmp_path):
        """带 group_id 时使用复合索引查询。"""
        from database.cache_store import CacheStore
        store = CacheStore(str(tmp_path / "test.db"))
        try:
            await store.init()
            # 插入测试数据
            await store.upsert_manifest(1, "fuid-1", 100, 500, "photo")
            await store.upsert_manifest(1, "fuid-1", 200, 600, "photo")
            await store.upsert_manifest(1, "fuid-2", 100, 700, "document")
            await store.upsert_manifest(2, "fuid-1", 300, 800, "photo")

            # 查询 group=1, fuid-1
            result = await store.get_manifest_by_file_unique_id("fuid-1", group_id=1)
            assert len(result) == 2
            channels = {r["channel_id"] for r in result}
            assert channels == {100, 200}
            # 所有记录的 file_unique_id 都应是 fuid-1
            assert all(r["file_unique_id"] == "fuid-1" for r in result)
            assert all(r["group_id"] == 1 for r in result)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_query_without_group_id(self, tmp_path):
        """不传 group_id 时跨组扫描。"""
        from database.cache_store import CacheStore
        store = CacheStore(str(tmp_path / "test.db"))
        try:
            await store.init()
            await store.upsert_manifest(1, "fuid-1", 100, 500, "photo")
            await store.upsert_manifest(2, "fuid-1", 300, 800, "photo")
            await store.upsert_manifest(1, "fuid-2", 100, 700, "document")

            # 跨组查询 fuid-1
            result = await store.get_manifest_by_file_unique_id("fuid-1", group_id=None)
            assert len(result) == 2
            groups = {r["group_id"] for r in result}
            assert groups == {1, 2}
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_query_no_match_returns_empty(self, tmp_path):
        """无匹配记录返回空列表。"""
        from database.cache_store import CacheStore
        store = CacheStore(str(tmp_path / "test.db"))
        try:
            await store.init()
            await store.upsert_manifest(1, "fuid-1", 100, 500, "photo")
            result = await store.get_manifest_by_file_unique_id("not-exists", group_id=1)
            assert result == []
        finally:
            await store.close()


# ════════════════════════════════════════════════════════════════
# 5. RelayInstance api_hash 日志指纹化测试
# ════════════════════════════════════════════════════════════════

class TestApiHashFingerprint:
    """P2-2: api_hash 日志指纹化测试。"""

    def test_relay_instance_start_uses_fingerprint(self, caplog):
        """RelayInstance.start 不应输出 api_hash 原值或前 10 位。"""
        try:
            from services.relay_instance import RelayInstance
        except Exception:
            pytest.skip("services.relay_instance 不可用")

        # 构造一个 RelayInstance(不真正连接)
        instance = RelayInstance.__new__(RelayInstance)
        instance.api_id = 12345
        instance.api_hash = "abcdef0123456789abcdef0123456789"
        instance.phone = "+1234567890"
        instance._session_path = "/tmp/test_session"
        instance._report_status = AsyncMock()
        instance._client = None
        instance._ready = MagicMock()

        import logging
        import asyncio
        from loguru import logger as loguru_logger

        # 捕获 loguru 日志
        messages = []

        def sink(message):
            messages.append(str(message.record["message"]))

        handler_id = loguru_logger.add(sink, level="INFO")

        try:
            # 运行 start()(会在 TelegramClient 构造前抛异常,因为我们没有 mock)
            # R43: Python 3.11+ 中 asyncio.get_event_loop() 无运行循环时弃用,
            # 显式创建新事件循环避免 DeprecationWarning/RuntimeError
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(instance.start())
            except Exception:
                pass  # 预期会失败,我们只关心日志
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

            # 验证日志中包含指纹而非原值
            start_logs = [m for m in messages if "启动登录" in m]
            assert len(start_logs) > 0, "应输出启动登录日志"
            log_msg = start_logs[0]
            # 不应包含原 api_hash 前 10 位
            assert "abcdef0123" not in log_msg, (
                "日志中包含 api_hash 前 10 位,违反指纹化要求"
            )
            # 不应包含完整 api_hash
            assert instance.api_hash not in log_msg, (
                "日志中包含完整 api_hash 原值"
            )
            # 应包含指纹标识
            assert "api_hash_fp=" in log_msg, "日志应包含 api_hash_fp 指纹字段"
        finally:
            loguru_logger.remove(handler_id)


# ════════════════════════════════════════════════════════════════
# 6. 架构文档 SHA 测试
# ════════════════════════════════════════════════════════════════

class TestArchitectureDocSha:
    """P2-5: 架构文档不应硬编码易过期的 commit SHA。"""

    def test_no_hardcoded_sha(self):
        """architecture-current.md 不应包含 9bb28a6 或其他长 SHA。"""
        import re
        from pathlib import Path
        doc_path = Path(__file__).resolve().parent.parent / "docs" / "architecture-current.md"
        if not doc_path.exists():
            pytest.skip("docs/architecture-current.md 不存在")
        content = doc_path.read_text(encoding="utf-8")
        # 不应包含已知的过期 SHA
        assert "9bb28a6" not in content, "架构文档仍包含过期 SHA 9bb28a6"
        # 不应包含 40 位完整 SHA(HEX 模式,排除 DDL_VERSION: 7 这种短数字)
        long_sha_pattern = re.compile(r"\b[0-9a-f]{40}\b")
        matches = long_sha_pattern.findall(content)
        assert not matches, f"架构文档包含长 SHA: {matches}"
