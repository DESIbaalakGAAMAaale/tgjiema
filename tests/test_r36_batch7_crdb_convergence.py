"""R36 Batch 7: CRDB 收口测试。

覆盖:
- H7-1: CockroachDBClient.connect() 使用 min_size=0 + application_name
- H7-2: Dsp sync_back_loop dirty 驱动 + 退避(无 dirty 时不连接 CRDB)
- H7-3: Admin count/list/search 走 SQLite read model(0 RU)
- H7-4: crdb_sync_service 模块可导入 + 入口函数签名正确
- H7-5: deploy_vps_per_bot.sh 含 migration oneshot + crdb_sync 服务定义
- H7-6: settings 默认值(min_size=0, max_size=2, application_name 前缀)
"""
import asyncio
import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


# ════════════════════════════════════════════════════════════════
# 1. Settings 默认值测试
# ════════════════════════════════════════════════════════════════

class TestSettingsDefaults:
    """R36 §6.4.1-2: 验证 settings 默认值符合 CRDB 收口要求。"""

    def test_crdb_pool_min_size_default_zero(self):
        """CRDB_POOL_MIN_SIZE 默认 0(空闲时关闭所有连接)。"""
        from config import settings
        assert settings.CRDB_POOL_MIN_SIZE == 0

    def test_crdb_pool_max_size_default_two(self):
        """CRDB_POOL_MAX_SIZE 默认 2(业务 Bot ≤2)。"""
        from config import settings
        assert settings.CRDB_POOL_MAX_SIZE == 2

    def test_crdb_application_name_prefix_default(self):
        """CRDB_APPLICATION_NAME_PREFIX 默认 tgjiema。"""
        from config import settings
        assert settings.CRDB_APPLICATION_NAME_PREFIX == "tgjiema"

    def test_application_name_format(self):
        """application_name 实际值为 f"{prefix}-{role}"。"""
        from config import settings
        role = settings.SERVICE_ROLE or "default"
        app_name = f"{settings.CRDB_APPLICATION_NAME_PREFIX}-{role}"
        assert app_name.startswith("tgjiema-")
        # 各角色格式
        for r in ("up", "idx", "dsp", "crdb_sync", "migration"):
            assert f"tgjiema-{r}" == f"{settings.CRDB_APPLICATION_NAME_PREFIX}-{r}"


# ════════════════════════════════════════════════════════════════
# 2. CockroachDBClient.connect() 测试(mock asyncpg.create_pool)
# ════════════════════════════════════════════════════════════════

# R36: database/session.py 使用 PEP 604 语法(list[str] | None),需要 Python 3.10+
# Python 3.9 环境下导入会失败,需要跳过这两个测试
def _session_module_importable() -> bool:
    try:
        import database.session as _s  # noqa: F401
        return inspect.isclass(getattr(_s, "CockroachDBClient", None))
    except Exception:
        return False


@pytest.mark.skipif(
    not _session_module_importable(),
    reason="database.session 不可用(需要 Python 3.10+ 或 asyncpg;PEP 604 语法)",
)
class TestCockroachDBClientConnect:
    """R36 §6.4.1-2: 验证 connect() 使用 min_size=0 + application_name。"""

    def test_connect_uses_min_size_zero(self):
        """connect() 不再 max(1, min_size),允许 min_size=0。"""
        from database.session import CockroachDBClient

        client = CockroachDBClient()
        client.configure("postgresql://test")

        captured_kwargs = {}

        async def mock_create_pool(*args, **kwargs):
            captured_kwargs.update(kwargs)
            mock_pool = MagicMock()
            mock_pool.close = AsyncMock()
            return mock_pool

        # 直接 monkey-patch _client._pool 避免触发 SQLite 缓存加载路径
        async def _run():
            with patch("database.session.asyncpg.create_pool", mock_create_pool):
                # patch get_cache_store 和 load_cache_from_disk 防止实际调用
                with patch("database.cache_store.get_cache_store") as mock_get_store:
                    mock_store = MagicMock()
                    mock_store.init = AsyncMock()
                    mock_store.get_kv = AsyncMock(return_value="8")
                    mock_get_store.return_value = mock_store
                    with patch("database.cache.load_cache_from_disk", AsyncMock()):
                        await client.connect()

        asyncio.run(_run())

        # 验证 min_size 来自 settings(默认 0),未被 max(1, ...) 强制为 1
        assert captured_kwargs.get("min_size") == 0, \
            f"min_size 应为 0(允许空闲关闭连接),实际: {captured_kwargs.get('min_size')}"
        # max_size 不超过 2
        assert captured_kwargs.get("max_size") <= 2, \
            f"max_size 应 ≤2,实际: {captured_kwargs.get('max_size')}"

    def test_connect_sets_application_name(self):
        """connect() 通过 server_settings 设置 application_name。"""
        from database.session import CockroachDBClient

        client = CockroachDBClient()
        client.configure("postgresql://test")

        captured_kwargs = {}

        async def mock_create_pool(*args, **kwargs):
            captured_kwargs.update(kwargs)
            mock_pool = MagicMock()
            mock_pool.close = AsyncMock()
            return mock_pool

        async def _run():
            with patch("database.session.asyncpg.create_pool", mock_create_pool):
                with patch("database.cache_store.get_cache_store") as mock_get_store:
                    mock_store = MagicMock()
                    mock_store.init = AsyncMock()
                    mock_store.get_kv = AsyncMock(return_value="8")
                    mock_get_store.return_value = mock_store
                    with patch("database.cache.load_cache_from_disk", AsyncMock()):
                        await client.connect()

        asyncio.run(_run())

        # server_settings 应包含 application_name
        server_settings = captured_kwargs.get("server_settings", {})
        assert "application_name" in server_settings, \
            f"server_settings 应包含 application_name,实际: {server_settings}"
        app_name = server_settings["application_name"]
        assert app_name.startswith("tgjiema-"), \
            f"application_name 应以 tgjiema- 开头,实际: {app_name}"


# ════════════════════════════════════════════════════════════════
# 3. crdb_sync_service 模块测试
# ════════════════════════════════════════════════════════════════

class TestCrdbSyncService:
    """R36 §6.3: crdb_sync 服务模块测试。"""

    def test_module_importable(self):
        """services.crdb_sync_service 模块可导入。"""
        from services import crdb_sync_service
        assert hasattr(crdb_sync_service, "main")
        assert hasattr(crdb_sync_service, "_sync_loop")
        assert hasattr(crdb_sync_service, "_sync_jobs")
        assert hasattr(crdb_sync_service, "_sync_cells")
        assert hasattr(crdb_sync_service, "_get_dirty_jobs")
        assert hasattr(crdb_sync_service, "_get_dirty_cells")

    def test_constants(self):
        """模块常量符合 R36 要求。"""
        from services import crdb_sync_service
        assert crdb_sync_service.DEFAULT_SYNC_INTERVAL == 60
        assert crdb_sync_service.MAX_BACKOFF == 1800

    @pytest.mark.asyncio
    async def test_sync_loop_with_dirty(self):
        """_sync_loop 有 dirty 时调用 sync_func + 重置退避。"""
        from services import crdb_sync_service as svc

        sync_call_count = 0

        async def mock_sync():
            nonlocal sync_call_count
            sync_call_count += 1

        async def mock_get_dirty():
            return [{"id": 1}]  # 非空 dirty

        sleep_count = 0

        async def mock_sleep(seconds):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise asyncio.CancelledError()

        # R38 P0-4: _sync_loop 现在每批前调用 _renew_leader_lease()(async,返回 bool)
        # 还会调用 _close_crdb_only / _acquire_leader_lease 等 R38 新增函数,
        # 全部 patch 成 no-op 协程,让 _sync_loop 顺利走到 sync_func 调用
        async def mock_true():
            return True

        async def mock_noop():
            return None

        with patch.object(svc.asyncio, "sleep", mock_sleep), \
             patch.object(svc, "_renew_leader_lease", mock_true), \
             patch.object(svc, "_acquire_leader_lease", mock_true), \
             patch.object(svc, "_close_crdb_only", mock_noop), \
             patch.object(svc, "_lazy_connect_crdb", mock_noop):
            try:
                await svc._sync_loop("test", mock_sync, mock_get_dirty, None)
            except asyncio.CancelledError:
                pass

        assert sync_call_count >= 1, "应至少调用一次 sync_func"

    @pytest.mark.asyncio
    async def test_sync_loop_without_dirty(self):
        """_sync_loop 无 dirty 时不调用 sync_func + 退避翻倍。"""
        from services import crdb_sync_service as svc

        sync_call_count = 0

        async def mock_sync():
            nonlocal sync_call_count
            sync_call_count += 1

        async def mock_get_dirty():
            return []  # 空 dirty

        sleep_count = 0

        async def mock_sleep(seconds):
            nonlocal sleep_count
            sleep_count += 1
            # 退避逻辑: 初始 backoff=60,无 dirty 时先翻倍再 sleep
            # 第一次循环: 60 → 120 → sleep(120)
            # 第二次循环: 120 → 240 → sleep(240)
            # 第三次循环: 240 → 480 → sleep(480)
            if sleep_count == 1:
                assert seconds == 120, f"第一次退避应为 120s(60翻倍),实际: {seconds}"
            elif sleep_count == 2:
                assert seconds == 240, f"第二次退避应为 240s(120翻倍),实际: {seconds}"
            if sleep_count >= 3:
                raise asyncio.CancelledError()

        with patch.object(svc.asyncio, "sleep", mock_sleep):
            try:
                await svc._sync_loop("test", mock_sync, mock_get_dirty, None)
            except asyncio.CancelledError:
                pass

        assert sync_call_count == 0, "无 dirty 时不应调用 sync_func"


# ════════════════════════════════════════════════════════════════
# 4. run_all.py 注册测试
# ════════════════════════════════════════════════════════════════

class TestRunAllRegistration:
    """R36 §6.3: 验证 crdb_sync 已注册到 BOT_RUNNERS。"""

    def test_crdb_sync_registered(self):
        """run_all.py BOT_RUNNERS 包含 crdb_sync。"""
        from run_all import BOT_RUNNERS
        assert "crdb_sync" in BOT_RUNNERS
        assert callable(BOT_RUNNERS["crdb_sync"])

    def test_run_crdb_sync_callable(self):
        """run_crdb_sync 函数可调用(不实际运行)。"""
        from run_all import run_crdb_sync
        assert callable(run_crdb_sync)


# ════════════════════════════════════════════════════════════════
# 5. deploy_vps_per_bot.sh 脚本内容测试
# ════════════════════════════════════════════════════════════════

class TestDeployScript:
    """R36 §6.3-6.4.3: 验证 deploy_vps_per_bot.sh 包含必要的服务定义。"""

    def test_deploy_script_has_crdb_sync_service(self):
        """deploy_vps_per_bot.sh SERVICES 数组包含 crdb_sync。"""
        script_path = Path(__file__).parent.parent / "deploy_vps_per_bot.sh"
        content = script_path.read_text(encoding="utf-8")
        assert "crdb_sync:CRDB同步服务" in content
        assert 'systemctl start "${SVC_PREFIX}-crdb_sync"' in content

    def test_deploy_script_has_migration_oneshot(self):
        """deploy_vps_per_bot.sh 包含 migration oneshot 服务定义。"""
        script_path = Path(__file__).parent.parent / "deploy_vps_per_bot.sh"
        content = script_path.read_text(encoding="utf-8")
        assert "migration.service" in content
        assert "Type=oneshot" in content
        assert "SERVICE_ROLE=migration" in content

    def test_deploy_script_has_secrets_isolation_for_crdb_sync(self):
        """crdb_sync 有独立的 secrets 隔离配置。"""
        script_path = Path(__file__).parent.parent / "deploy_vps_per_bot.sh"
        content = script_path.read_text(encoding="utf-8")
        assert '[crdb_sync]="COCKROACHDB_URL"' in content

    def test_deploy_script_target_includes_crdb_sync(self):
        """target 的 Wants= 包含 crdb_sync.service。"""
        script_path = Path(__file__).parent.parent / "deploy_vps_per_bot.sh"
        content = script_path.read_text(encoding="utf-8")
        assert "crdb_sync.service" in content


# ════════════════════════════════════════════════════════════════
# 6. shared_counters 包含 total_logs 测试
# ════════════════════════════════════════════════════════════════

class TestSharedCounters:
    """R36 §6.4.5: shared_counters 包含 total_logs 默认值。"""

    def test_total_logs_default_zero(self):
        """status_counters 默认包含 total_logs=0。"""
        from utils import shared_counters as _sc
        assert "total_logs" in _sc.status_counters
        assert _sc.status_counters["total_logs"] == 0

    def test_total_logs_settable(self):
        """total_logs 可被设置。"""
        from utils import shared_counters as _sc
        original = _sc.status_counters.get("total_logs", 0)
        try:
            _sc.status_counters["total_logs"] = 12345
            assert _sc.status_counters["total_logs"] == 12345
        finally:
            _sc.status_counters["total_logs"] = original


# ════════════════════════════════════════════════════════════════
# 7. cache_store SQLite read model 测试(需要 CacheStore 真实类)
# ════════════════════════════════════════════════════════════════

# 延迟检查 CacheStore 是否可用(Python 3.10+ + aiosqlite)
def _cache_store_available() -> bool:
    try:
        from database import cache_store as _cs
        return inspect.isclass(_cs.CacheStore)
    except Exception:
        return False


@pytest.mark.skipif(
    not _cache_store_available(),
    reason="database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
)
class TestSQLiteReadModel:
    """R36 §6.4.5: Admin 走 SQLite read model 测试。"""

    @pytest_asyncio.fixture
    async def real_store(self):
        """创建一个使用临时文件数据库的 CacheStore 实例。"""
        from database import cache_store as _cs_module
        tmpdir = tempfile.mkdtemp(prefix="r36_batch7_test_")
        db_path = Path(tmpdir) / "test_cache.db"
        original_path = _cs_module.DB_PATH
        _cs_module.DB_PATH = db_path
        try:
            s = _cs_module.CacheStore()
            await s.init()
            yield s
            await s.close()
        finally:
            _cs_module.DB_PATH = original_path
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_count_users_local_empty(self, real_store):
        """空表 count_users_local 返回 0。"""
        count = await real_store.count_users_local()
        assert count == 0

    @pytest.mark.asyncio
    async def test_count_users_local_with_data(self, real_store):
        """有数据时 count_users_local 正确计数。"""
        await real_store.upsert_user_local({"user_id": 1001, "username": "alice"})
        await real_store.upsert_user_local({"user_id": 1002, "username": "bob"})
        await real_store.upsert_user_local({"user_id": 1003, "username": "alice2"})
        count = await real_store.count_users_local()
        assert count == 3

    @pytest.mark.asyncio
    async def test_count_users_local_search_by_id(self, real_store):
        """按 user_id(数字)搜索计数。"""
        await real_store.upsert_user_local({"user_id": 1001, "username": "alice"})
        await real_store.upsert_user_local({"user_id": 1002, "username": "bob"})
        count = await real_store.count_users_local(search="1001")
        assert count == 1

    @pytest.mark.asyncio
    async def test_count_users_local_search_by_name(self, real_store):
        """按 username/first_name LIKE 搜索计数。"""
        await real_store.upsert_user_local({
            "user_id": 1001, "username": "alice", "first_name": "Alice",
        })
        await real_store.upsert_user_local({
            "user_id": 1002, "username": "bob", "first_name": "Bob",
        })
        await real_store.upsert_user_local({
            "user_id": 1003, "username": "alice2", "first_name": "Alice2",
        })
        count = await real_store.count_users_local(search="alice")
        assert count == 2  # alice + alice2

    @pytest.mark.asyncio
    async def test_list_users_local_paginated(self, real_store):
        """分页 + 排序测试。"""
        for i in range(5):
            await real_store.upsert_user_local({
                "user_id": 1000 + i,
                "username": f"user{i}",
                "created_at": f"2026-01-{i+1:02d}T00:00:00",
            })
        result = await real_store.list_users_local_paginated(
            skip=0, limit=3, sort_field="user_id", sort_dir="asc",
        )
        assert len(result) == 3
        assert result[0]["user_id"] == 1000
        assert result[1]["user_id"] == 1001
        assert result[2]["user_id"] == 1002

    @pytest.mark.asyncio
    async def test_list_users_local_paginated_search(self, real_store):
        """带搜索的分页查询。"""
        await real_store.upsert_user_local({"user_id": 1001, "username": "alice"})
        await real_store.upsert_user_local({"user_id": 1002, "username": "bob"})
        result = await real_store.list_users_local_paginated(search="alice")
        assert len(result) == 1
        assert result[0]["username"] == "alice"

    @pytest.mark.asyncio
    async def test_list_users_local_sort_field_whitelist(self, real_store):
        """非白名单排序字段回退到 created_at(防 SQL 注入)。"""
        await real_store.upsert_user_local({"user_id": 1001, "username": "alice"})
        # 尝试注入 SQL
        result = await real_store.list_users_local_paginated(
            sort_field="; DROP TABLE users_local; --",
        )
        # 不应抛异常,且返回正确数据
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_count_file_records_local(self, real_store):
        """file_records count 测试。"""
        await real_store.upsert_file_record_local({
            "file_code": "ABC001", "status": "active", "uploader_id": 1001,
        })
        await real_store.upsert_file_record_local({
            "file_code": "ABC002", "status": "active", "uploader_id": 1002,
        })
        await real_store.upsert_file_record_local({
            "file_code": "ABC003", "status": "deleted", "uploader_id": 1001,
        })
        # 全部
        assert await real_store.count_file_records_local() == 3
        # 按状态
        assert await real_store.count_file_records_local(status="active") == 2
        assert await real_store.count_file_records_local(status="deleted") == 1
        # 按 uploader_id 搜索
        assert await real_store.count_file_records_local(search="1001") == 2

    @pytest.mark.asyncio
    async def test_list_file_records_local_paginated(self, real_store):
        """file_records 分页查询。"""
        for i in range(3):
            await real_store.upsert_file_record_local({
                "file_code": f"FILE{i:03d}",
                "status": "active",
                "uploader_id": 1001,
            })
        result = await real_store.list_file_records_local_paginated(
            skip=0, limit=2, sort_field="file_code", sort_dir="asc",
        )
        assert len(result) == 2
        assert result[0]["file_code"] == "FILE000"
        assert result[1]["file_code"] == "FILE001"

    @pytest.mark.asyncio
    async def test_list_file_records_local_search_by_code(self, real_store):
        """file_code LIKE 搜索。"""
        await real_store.upsert_file_record_local({
            "file_code": "TEST001", "status": "active", "uploader_id": 1001,
        })
        await real_store.upsert_file_record_local({
            "file_code": "PROD001", "status": "active", "uploader_id": 1002,
        })
        result = await real_store.list_file_records_local_paginated(search="TEST")
        assert len(result) == 1
        assert result[0]["file_code"] == "TEST001"
