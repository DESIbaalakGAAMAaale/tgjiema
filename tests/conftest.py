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
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _install_fake_config() -> None:
    """注入模拟 config 模块(覆盖可能存在的真实 config,确保测试隔离)。

    被测代码仅读取 REDIS_URL / WRITER_MODE / WRITER_QUEUE_KEY 等少量属性,
    其它属性即便未被显式设置,MagicMock 也不会影响这些用例。
    """
    settings = MagicMock(name="test_settings")
    # 与 config/settings.py 默认值保持一致
    settings.REDIS_URL = ""                  # 默认空 → 降级模式;用例按需覆盖为非空启用 Redis
    settings.WRITER_MODE = "redis"
    settings.WRITER_QUEUE_KEY = "tgjiema:writer:queue"
    settings.WRITER_BATCH_SIZE = 10
    settings.WRITER_QUEUE_ALERT_THRESHOLD = 1000
    settings.WRITER_READ_CACHE_TTL = 5
    settings.CRDB_POOL_MIN_SIZE = 1
    settings.CRDB_POOL_MAX_SIZE = 5

    fake_config = types.ModuleType("config")
    fake_config.settings = settings
    # 强制覆盖,确保被测模块函数内的 ``from config import settings`` 拿到模拟对象
    sys.modules["config"] = fake_config


def _ensure_tested_modules_importable() -> None:
    """确保被测模块 database.redis_queue / database.write_router 可导入。

    优先走正常导入路径。若 ``database/__init__.py`` 因环境问题无法导入
    (例如其依赖的 ``session.py`` 使用了 Python 3.10+ 语法、或缺失 asyncpg 等
    运行时依赖),则降级为:构造轻量 ``database`` 包占位对象,按文件路径直接
    加载 ``redis_queue.py`` 与 ``write_router.py`` 两个子模块并注册到 sys.modules,
    从而绕过重 ``__init__``。被测的两个模块本身只依赖 loguru,不依赖 session。
    """
    try:
        importlib.import_module("database.redis_queue")
        importlib.import_module("database.write_router")
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
        # 将子模块挂到包对象上,供 ``from database import redis_queue`` 解析
        setattr(db_pkg, mod_name, module)


# 收集阶段即注入(早于任何 test 模块 import 被测代码)
_install_fake_config()
_ensure_tested_modules_importable()


@pytest.fixture(autouse=True)
def reset_redis_queue_state():
    """每个用例前重置 redis_queue 模块全局缓存状态,避免上一个用例的连接缓存影响下一个。"""
    import database.redis_queue as rq
    rq._redis_client = None
    rq._redis_available = False
    rq._redis_init_attempted = False
    rq._redis_last_attempt_ts = 0.0
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
    """返回一个 AsyncMock 的 Redis 客户端,默认方法返回值贴合真实 redis.asyncio 语义。"""
    client = AsyncMock(name="mock_redis_client")
    client.ping = AsyncMock(return_value=True)
    client.lpush = AsyncMock(return_value=1)
    client.brpop = AsyncMock(return_value=None)    # 默认超时返回 None
    client.delete = AsyncMock(return_value=1)
    client.llen = AsyncMock(return_value=0)
    client.get = AsyncMock(return_value=None)      # 默认缓存未命中
    client.setex = AsyncMock(return_value=True)
    client.aclose = AsyncMock(return_value=None)
    return client
