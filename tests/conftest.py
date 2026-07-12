"""pytest 公共配置。

职责:
1. 在测试收集阶段向 sys.modules 注入模拟 config 模块,绕过
   ``config.settings.Settings`` 的严格环境变量校验(被测模块
   ``database.redis_queue`` / ``database.write_router`` 均在函数内部
   懒加载 ``from config import settings``,只需在 sys.modules 预置
   一个带必要属性的模拟对象即可让被测代码正常运行)。
2. 提供 ``redis_queue`` 模块全局状态重置的 autouse fixture,避免跨用例污染。
3. 提供 ``mock_settings`` / ``mock_redis`` 共享 fixture。
4. 异步用例统一用 ``@pytest.mark.asyncio`` 装饰(兼容 pytest-asyncio strict 模式,
   无需依赖 asyncio_mode=auto)。

R33 修复: mock 配置从 Redis List (BRPOP/LPOP) 改为 Streams
   (XADD/XREADGROUP/XACK/XAUTOCLAIM/XPENDING/XLEN/XGROUP CREATE)。
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _install_fake_config() -> None:
    """注入模拟 config 模块(覆盖可能存在的真实 config,确保测试隔离)。

    被测代码仅读取 REDIS_URL / WRITER_MODE / WRITER_STREAM_KEY 等少量属性,
    其它属性即便未被显式设置,MagicMock 也不会影响这些用例。

    R33: 配置从 List 改为 Streams(Consumer Group / XAUTOCLAIM / 死信重试)。
    """
    settings = MagicMock(name="test_settings")
    # 与 config/settings.py 默认值保持一致
    settings.REDIS_URL = ""                  # 默认空 → 降级模式;用例按需覆盖为非空启用 Redis
    settings.WRITER_MODE = "redis"
    # R33: Stream 配置(替代 List)
    settings.WRITER_STREAM_KEY = "tgjiema:writer:stream"
    settings.WRITER_CONSUMER_GROUP = "tgjiema-writer-group"
    settings.WRITER_CONSUMER_NAME = "db_writer"
    settings.WRITER_RECLAIM_IDLE_MS = 30000
    settings.WRITER_BATCH_SIZE = 10
    settings.WRITER_QUEUE_ALERT_THRESHOLD = 1000
    # R33: 死信队列配置
    settings.WRITER_DEAD_STREAM_KEY = "tgjiema:writer:dead"
    settings.WRITER_DEAD_MAX_ATTEMPTS = 3
    settings.WRITER_DEAD_RETRY_DELAY = 60
    settings.DB_WRITER_SERVICE_NAME = "tgjiema-db_writer"
    # P2修复: 分级 TTL 配置
    settings.WRITER_CACHE_TTL_QUOTA = 5
    settings.WRITER_CACHE_TTL_FILE_RECORD = 30
    settings.WRITER_CACHE_TTL_CODE = 30
    settings.WRITER_CACHE_TTL_USER = 30
    settings.WRITER_CACHE_TTL_CELLS = 10
    settings.WRITER_CACHE_TTL_BOT_HB = 5
    settings.WRITER_CACHE_TTL_KV = 60
    settings.CRDB_POOL_MIN_SIZE = 1
    settings.CRDB_POOL_MAX_SIZE = 5

    fake_config = types.ModuleType("config")
    fake_config.settings = settings
    # 强制覆盖,确保被测模块函数内的 ``from config import settings`` 拿到模拟对象
    sys.modules["config"] = fake_config


def _ensure_tested_modules_importable() -> None:
    """确保被测模块 database.redis_queue / database.write_router / database.db_writer 可导入。

    优先走正常导入路径。若 ``database/__init__.py`` 因环境问题无法导入
    (例如其依赖的 ``session.py`` 使用了 Python 3.10+ 语法、或缺失 asyncpg 等
    运行时依赖),则降级为:构造轻量 ``database`` 包占位对象,按文件路径直接
    加载 ``redis_queue.py`` / ``write_router.py`` / ``db_writer.py`` 三个子模块
    并注册到 sys.modules,从而绕过重 ``__init__``。

    db_writer 依赖 cache_store.CacheStore / DB_PATH,若 cache_store 因
    aiosqlite 等依赖缺失无法加载,创建 mock 占位对象让 db_writer 可导入
    (测试中 _execute_sqlite 被 mock,不需要真实 CacheStore 实现)。
    """
    try:
        importlib.import_module("database.redis_queue")
        importlib.import_module("database.write_router")
        # 也尝试导入 db_writer(可能因 cache_store 依赖失败)
        try:
            importlib.import_module("database.db_writer")
        except Exception:
            pass  # 降级路径会处理
        return
    except Exception:
        # 正常导入失败 → 走降级路径,避免阻塞测试收集
        pass

    db_dir = Path(__file__).resolve().parent.parent / "database"
    # 构造轻量 database 包(不执行真实 __init__.py)
    db_pkg = types.ModuleType("database")
    db_pkg.__path__ = [str(db_dir)]
    sys.modules["database"] = db_pkg

    # 按依赖顺序直接加载子模块:redis_queue 在前(write_router 依赖它)
    for mod_name, file_name in (
        ("redis_queue", "redis_queue.py"),
        ("write_router", "write_router.py"),
    ):
        full_name = "database." + mod_name
        spec = importlib.util.spec_from_file_location(full_name, db_dir / file_name)
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        setattr(db_pkg, mod_name, module)

    # 尝试加载 cache_store(db_writer 依赖它);失败则创建 mock 占位
    cache_store_name = "database.cache_store"
    try:
        spec = importlib.util.spec_from_file_location(cache_store_name, db_dir / "cache_store.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[cache_store_name] = module
        spec.loader.exec_module(module)
        setattr(db_pkg, "cache_store", module)
    except Exception:
        # cache_store 依赖 aiosqlite 等,测试环境可能缺失
        # 创建 mock 占位:db_writer 只需 CacheStore 类和 DB_PATH 属性
        from unittest.mock import MagicMock
        mock_cs = types.ModuleType("database.cache_store")
        mock_cs.CacheStore = MagicMock(name="MockCacheStore")
        mock_cs.DB_PATH = db_dir.parent / "data" / "cache_store.db"
        sys.modules[cache_store_name] = mock_cs
        setattr(db_pkg, "cache_store", mock_cs)

    # 加载 db_writer(cache_store 已就绪)
    try:
        spec = importlib.util.spec_from_file_location("database.db_writer", db_dir / "db_writer.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["database.db_writer"] = module
        spec.loader.exec_module(module)
        setattr(db_pkg, "db_writer", module)
    except Exception:
        pass  # db_writer 加载失败时,importorskip 会优雅跳过

    # R34 P1-1: 加载 dlq_worker(redis_queue 已就绪)
    try:
        spec = importlib.util.spec_from_file_location("database.dlq_worker", db_dir / "dlq_worker.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["database.dlq_worker"] = module
        spec.loader.exec_module(module)
        setattr(db_pkg, "dlq_worker", module)
    except Exception:
        pass  # dlq_worker 加载失败时,importorskip 会优雅跳过


# 收集阶段即注入(早于任何 test 模块 import 被测代码)
_install_fake_config()
_ensure_tested_modules_importable()


@pytest.fixture(autouse=True)
def reset_redis_queue_state():
    """每个用例前重置 redis_queue 模块全局缓存状态,避免上一个用例的连接缓存影响下一个。

    R33: 也重置 _consumer_group_ensured 标志,确保 ensure_consumer_group
    在每个用例中都能被重新调用。
    """
    import database.redis_queue as rq
    rq._redis_client = None
    rq._redis_available = False
    rq._redis_init_attempted = False
    rq._redis_last_attempt_ts = 0.0
    # P1修复: 重置 asyncio Lock,避免跨事件循环复用导致死锁
    rq._redis_init_lock = None
    # R33: 重置 Consumer Group 已创建标志
    rq._consumer_group_ensured = False
    yield


@pytest.fixture
def mock_settings():
    """返回共享的模拟 settings 对象。

    用例可通过 ``monkeypatch.setattr(mock_settings, "REDIS_URL", "...")`` 修改属性,
    用例结束自动还原。
    """
    import config
    return config.settings


@pytest.fixture
def mock_redis():
    """返回一个 AsyncMock 的 Redis 客户端,默认方法返回值贴合真实 redis.asyncio 语义。

    R33: 从 List API (lpush/brpop/lpop/rpush/llen) 改为 Streams API
         (xadd/xreadgroup/xack/xautoclaim/xpending/xlen/xgroup_create)。
    """
    client = AsyncMock(name="mock_redis_client")
    client.ping = AsyncMock(return_value=True)
    # R33: Stream 写入(XADD 返回消息 ID)
    client.xadd = AsyncMock(return_value="1700000000000-0")
    # R33: Consumer Group 消费(XREADGROUP 返回 [(stream, [(msg_id, fields), ...])])
    client.xreadgroup = AsyncMock(return_value=[])
    # R33: Consumer Group 创建(XGROUP CREATE,BUSYGROUP 时用 side_effect 模拟)
    client.xgroup_create = AsyncMock(return_value=True)
    # R33: 确认消息(XACK 返回确认数)
    client.xack = AsyncMock(return_value=1)
    # R33: 回收 pending(XAUTOCLAIM 返回 (next_id, [(msg_id, fields), ...], []))
    client.xautoclaim = AsyncMock(return_value=("0-0", [], []))
    # R33: pending 信息(XPENDING 返回 (count, min_id, max_id, consumers))
    client.xpending = AsyncMock(return_value=(0, None, None, []))
    # R33: Stream 长度(XLEN)
    client.xlen = AsyncMock(return_value=0)
    # R34 P1-1: 死信队列读取(XRANGE)与删除(XDEL)
    client.xrange = AsyncMock(return_value=[])
    client.xdel = AsyncMock(return_value=1)
    # 通用 key 操作(读缓存 DEL/GET/SETEX)
    client.delete = AsyncMock(return_value=1)
    client.get = AsyncMock(return_value=None)      # 默认缓存未命中
    client.setex = AsyncMock(return_value=True)
    client.aclose = AsyncMock(return_value=None)
    return client
