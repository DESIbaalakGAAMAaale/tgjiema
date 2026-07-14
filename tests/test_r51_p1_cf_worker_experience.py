"""R51 P1 CF Worker + i18n + 按钮体验测试。

测试覆盖矩阵(16 用例):

A. CF Worker 静态检查(7 测试)
   1.  test_cf_worker_file_exists
      — cf-workers/file-bot/src/index.js 文件存在
   2.  test_cf_worker_contains_locale_messages
      — 包含 LOCALE_MESSAGES 常量(中英文文案)
   3.  test_cf_worker_contains_update_id_kv_dedup
      — 包含 UPDATE_ID_KV 持久去重逻辑
   4.  test_cf_worker_contains_max_body_bytes
      — 包含 MAX_BODY_BYTES(1MB 请求体上限)
   5.  test_cf_worker_contains_schema_validation
      — 包含 isValidTelegramUpdate schema 校验
   6.  test_cf_worker_no_memory_debounce
      — 不含 isDebounced / debounce.get(内存 debounce 已移除)
   7.  test_cf_worker_sendmessage_no_body_logging
      — sendMessage 失败时只记录 HTTP status,不记录 body

B. i18n LRU 缓存(5 测试)
   8.  test_i18n_cache_hit_no_sqlite_read
      — 缓存命中时不触发 SQLite 读取
   9.  test_i18n_cache_ttl_expiry_triggers_reload
      — TTL 过期后触发重新加载
   10. test_i18n_invalidate_cache_clears_entry
      — invalidate_user_locale_cache 清除指定条目
   11. test_i18n_get_user_locale_async_works
      — get_user_locale_async 异步版本正常工作
   12. test_i18n_set_user_locale_invalidates_cache
      — set_user_locale 成功后主动失效缓存

C. 按钮安全(4 测试)
   13. test_generate_signed_callback_warns_for_high_risk
      — 高风险 action 通过 generate_signed_callback 签名时记录 warning
   14. test_generate_signed_callback_no_warning_for_low_risk
      — 低风险 action 不记录 warning
   15. test_sign_and_verify_button_token_with_nonce
      — sign_button_token_with_nonce + verify_button_token 端到端
   16. test_verify_button_token_rejects_replay
      — 同一 token 第二次验证被拒(nonce 已消费)

依赖:
- cf-workers/file-bot/src/index.js(P1-8 重写)
- services/i18n.py(P1-9 LRU 缓存)
- services/button_security.py(P1-10 高风险 action 警告)
- services/error_codes.py(AppError + ErrorCodes 协议)
- database/cache_store.py(真实 SQLite 临时 DB,用于 nonce 持久化)
"""
from __future__ import annotations

import inspect
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

# mock telegram(与 conftest.py 协同)
import sys
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
CF_WORKER_PATH = REPO_ROOT / "cf-workers" / "file-bot" / "src" / "index.js"


# ── 模块级 skip 检查:cache_store 必须是真实类(非 conftest 降级 MagicMock)──
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ════════════════════════════════════════════════════════════════
# Fixture: i18n 单例 + 缓存重置(用例间隔离)
# ════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def reset_i18n_state():
    """每个用例前重置 services.i18n 模块级单例和 LRU 缓存,避免跨用例污染。"""
    try:
        import services.i18n as i18n_mod
        old_manager = i18n_mod._i18n_manager
        i18n_mod._i18n_manager = None
        i18n_mod.invalidate_user_locale_cache(None)
        # 重置异步锁(避免跨事件循环复用)
        i18n_mod._locale_cache_lock = None
        yield
        i18n_mod._i18n_manager = old_manager
        i18n_mod.invalidate_user_locale_cache(None)
        i18n_mod._locale_cache_lock = None
    except ImportError:
        yield


# ════════════════════════════════════════════════════════════════
# Fixture: 固定 BOT_TOKEN(避免 MagicMock 干扰 HMAC 签名)
# ════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def setup_bot_token(monkeypatch):
    """为 button_security 提供固定 BOT_TOKEN(避免 MagicMock 导致 HMAC 失败)。

    conftest 注入的 settings 是 MagicMock,ADMIN_BOT_TOKEN 属性也是 MagicMock,
    调用 .encode() 会返回 MagicMock 导致 hmac.new() 抛错。
    此处将其设为固定字符串,确保 _sign() 可正常工作。
    """
    import config
    monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "r51_test_admin_bot_token")
    monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "r51_test_sender_bot_token")
    # 默认 development 环境(避免触发 production fail-closed)
    monkeypatch.setattr(config.settings, "ENVIRONMENT", "development")


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库(用于 nonce 持久化 + i18n set_user_locale)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离于生产 cache_store.db)。

    策略(参考 test_r50_p1_2_callback_allowlist_replay.py):
    1. 临时目录下的 test_r51_p1_cf_worker.db
    2. 替换 database.cache_store.DB_PATH 指向临时路径
    3. 替换 database.cache_store.get_cache_store 返回测试 store
       (sign_button_token_with_nonce 内部调用 _cs.get_cache_store())
    4. 结束后恢复 + close + shutil.rmtree
    """
    tmpdir = tempfile.mkdtemp(prefix="r51_p1_test_")
    db_path = Path(tmpdir) / "test_r51_p1_cf_worker.db"
    original_path = _cs_module.DB_PATH
    original_get_store = _cs_module.get_cache_store
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        # 让 button_security 内的 get_cache_store() 返回测试 store
        _cs_module.get_cache_store = lambda: s
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module.get_cache_store = original_get_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def temp_i18n_db(monkeypatch, tmp_path):
    """创建带 users_local + dirty_outbox 表的临时 SQLite DB(用于 set_user_locale 测试)。

    返回临时 DB 路径,测试结束后 tmp_path 自动清理。
    """
    db_path = tmp_path / "test_i18n_locale.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users_local "
        "(user_id INTEGER PRIMARY KEY, locale TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dirty_outbox "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
        " table_name TEXT, pk TEXT, version INTEGER, "
        " operation TEXT, payload TEXT, created_at TEXT, "
        " processed INTEGER, local_only INTEGER)"
    )
    # 插入测试用户
    conn.execute(
        "INSERT INTO users_local (user_id, locale) VALUES (?, ?)",
        (50001, "zh-CN"),
    )
    conn.commit()
    conn.close()

    # Monkeypatch DB_PATH(让 i18n.py 的 set_user_locale / _load_user_locale_from_sqlite 使用临时 DB)
    monkeypatch.setattr(_cs_module, "DB_PATH", db_path)
    return db_path


# ════════════════════════════════════════════════════════════════
# A. CF Worker 静态检查(7 测试)
# ════════════════════════════════════════════════════════════════

class TestCfWorkerStaticChecks:
    """A 节: CF Worker (cf-workers/file-bot/src/index.js) 静态检查。

    由于 CF Worker 运行在 Cloudflare Workers 环境(Node.js + Workers runtime),
    无法在 Python pytest 中直接执行,因此采用静态检查策略:
    - 文件存在性
    - 关键字符串存在/不存在(验证 R51 P1-8 整改内容)
    """

    def test_cf_worker_file_exists(self):
        """A1: cf-workers/file-bot/src/index.js 文件存在。"""
        assert CF_WORKER_PATH.exists(), f"CF Worker 文件不存在: {CF_WORKER_PATH}"
        assert CF_WORKER_PATH.is_file(), f"路径不是文件: {CF_WORKER_PATH}"

    def test_cf_worker_contains_locale_messages(self):
        """A2: 包含 LOCALE_MESSAGES 常量(中英文文案已从硬编码改为 locale 常量)。"""
        content = CF_WORKER_PATH.read_text(encoding="utf-8")
        # LOCALE_MESSAGES 常量定义
        assert "const LOCALE_MESSAGES" in content, (
            "应定义 LOCALE_MESSAGES 常量(R51 P1-8: 用户文案改为从 locale 读取)"
        )
        # 中英文 locale 字典
        assert '"zh-CN"' in content, "LOCALE_MESSAGES 应包含 zh-CN 字典"
        assert '"en-US"' in content, "LOCALE_MESSAGES 应包含 en-US 字典"
        # detectLocale 函数
        assert "function detectLocale" in content, (
            "应定义 detectLocale 函数(根据 msg.from.language_code 推断 locale)"
        )
        # t() 翻译函数
        assert "function t(" in content, "应定义 t() locale 消息查找函数"

    def test_cf_worker_contains_update_id_kv_dedup(self):
        """A3: 包含 UPDATE_ID_KV 持久去重逻辑(替代内存 debounce)。

        R52 P1-8: 两阶段去重(processing → completed)替代单阶段标记。
        - markUpdateIdProcessing: handler 执行前标记 processing(TTL 5min,
          崩溃后可快速回收重试)
        - markUpdateIdCompleted: handler 成功后标记 completed(TTL 24h,
          防止 Telegram 重发)
        """
        content = CF_WORKER_PATH.read_text(encoding="utf-8")
        # KV 绑定引用
        assert "UPDATE_ID_KV" in content, (
            "应引用 env.UPDATE_ID_KV(R51 P1-8: 使用 KV 持久去重 update_id)"
        )
        # 去重函数:检查 update_id 是否已 completed
        assert "isUpdateIdProcessed" in content, (
            "应定义 isUpdateIdProcessed 函数(检查 update_id 是否已 completed)"
        )
        # R52 P1-8: 两阶段去重函数(processing + completed)
        assert "markUpdateIdProcessing" in content, (
            "应定义 markUpdateIdProcessing 函数"
            "(R52 P1-8: handler 执行前标记 processing,TTL 5min)"
        )
        assert "markUpdateIdCompleted" in content, (
            "应定义 markUpdateIdCompleted 函数"
            "(R52 P1-8: handler 成功后标记 completed,TTL 24h)"
        )
        # KV TTL 常量
        assert "UPDATE_ID_KV_TTL_SECONDS" in content, (
            "应定义 UPDATE_ID_KV_TTL_SECONDS(KV 过期时间)"
        )
        # R52 P1-8: processing 状态 TTL 常量(较短,崩溃后可快速回收)
        assert "UPDATE_ID_PROCESSING_TTL_SECONDS" in content, (
            "应定义 UPDATE_ID_PROCESSING_TTL_SECONDS(R52 P1-8: processing TTL)"
        )

    def test_cf_worker_contains_max_body_bytes(self):
        """A4: 包含 MAX_BODY_BYTES(1MB 请求体上限)。"""
        content = CF_WORKER_PATH.read_text(encoding="utf-8")
        assert "MAX_BODY_BYTES" in content, (
            "应定义 MAX_BODY_BYTES 常量(R51 P1-8: 请求 body 大小上限)"
        )
        # 1MB = 1 * 1024 * 1024
        assert "1024 * 1024" in content, (
            "MAX_BODY_BYTES 应为 1MB(1 * 1024 * 1024)"
        )
        # Content-Length 检查
        assert "content-length" in content.lower(), (
            "应检查 Content-Length 请求头(防止超大 body)"
        )
        # 413 Payload Too Large 响应
        assert "413" in content, (
            "超大 body 应返回 413 Payload Too Large"
        )

    def test_cf_worker_contains_schema_validation(self):
        """A5: 包含 isValidTelegramUpdate schema 校验函数。"""
        content = CF_WORKER_PATH.read_text(encoding="utf-8")
        assert "function isValidTelegramUpdate" in content, (
            "应定义 isValidTelegramUpdate 函数(R51 P1-8: 基本 schema 校验)"
        )
        # 校验 message / edited_message / channel_post / callback_query 之一
        assert "message" in content
        assert "edited_message" in content
        assert "callback_query" in content
        # 400 Bad Request 响应(schema 不合法)
        assert "400" in content, "schema 不合法应返回 400 Bad Request"

    def test_cf_worker_no_memory_debounce(self):
        """A6: 不含 isDebounced / debounce.get(内存 debounce 已移除)。

        R51 P1-8: 内存 debounce 在多 isolate 环境下不可靠(不同 isolate 不共享内存),
        已改用 KV 持久化 update_id 去重。
        """
        content = CF_WORKER_PATH.read_text(encoding="utf-8")
        # 不应包含 isDebounced 函数定义或调用
        assert "isDebounced" not in content, (
            "不应包含 isDebounced(R51 P1-8: 内存 debounce 已移除)"
        )
        # 不应包含 debounce.get(Map 方法调用)
        assert "debounce.get(" not in content, (
            "不应包含 debounce.get((内存 Map 已移除)"
        )
        # 不应包含 const debounce = new Map
        assert "const debounce = new Map" not in content, (
            "不应包含 const debounce = new Map(内存 Map 已移除)"
        )
        # 注:注释中提到 "debounce" 是允许的(解释移除原因)

    def test_cf_worker_sendmessage_no_body_logging(self):
        """A7: sendMessage 失败时只记录 HTTP status,不记录 body。

        R51 P1-8: Telegram API 错误 body 可能含敏感信息(chat_id, user_id),
        不应原样 console.error。
        """
        content = CF_WORKER_PATH.read_text(encoding="utf-8")
        # 应使用 console.warn(不是 console.error)记录失败
        assert "console.warn" in content, (
            "应使用 console.warn 记录错误(R51 P1-8: catch 块添加日志)"
        )
        # sendMessage 失败时只记录 HTTP status
        assert "HTTP ${resp.status}" in content, (
            "sendMessage 失败时应只记录 HTTP status code,不记录 body"
        )
        # 不应在 sendMessage 中读取 body 并记录
        # (检查不包含 await resp.text() 后直接 console.error 的模式)
        send_message_section = content[content.index("async function sendMessage"):]
        # 找到函数结束(下一个 async function 或 export)
        next_func_idx = send_message_section.find("\nasync function", 1)
        if next_func_idx == -1:
            next_func_idx = send_message_section.find("\nexport default")
        send_message_body = send_message_section[:next_func_idx] if next_func_idx > 0 else send_message_section
        # 不应在 sendMessage 函数体中 console.error 记录 body
        assert "console.error" not in send_message_body, (
            "sendMessage 中不应使用 console.error(R51 P1-8: 改用 console.warn)"
        )


# ════════════════════════════════════════════════════════════════
# B. i18n LRU 缓存(5 测试)
# ════════════════════════════════════════════════════════════════

class TestI18nLruCache:
    """B 节: services/i18n.py 的 LRU 缓存(R51 P1-9)。

    验证:
    - get_user_locale 缓存命中时不触发 SQLite 读取
    - TTL 过期后触发重新加载
    - invalidate_user_locale_cache 清除指定条目
    - get_user_locale_async 异步版本正常工作
    - set_user_locale 成功后主动失效缓存
    """

    def test_i18n_cache_hit_no_sqlite_read(self, monkeypatch):
        """B8: 缓存命中时不触发 SQLite 读取(第二次调用直接返回缓存值)。"""
        import services.i18n as i18n_mod

        manager = i18n_mod.get_i18n_manager()
        # 记录 _load_user_locale_from_sqlite 调用次数
        call_count = {"count": 0}
        original_load = manager._load_user_locale_from_sqlite

        def counting_load(user_id):
            call_count["count"] += 1
            return "en-US"

        monkeypatch.setattr(manager, "_load_user_locale_from_sqlite", counting_load)

        # 第一次调用:缓存 miss,触发 SQLite 读取
        result1 = manager.get_user_locale(60001)
        assert result1 == "en-US"
        assert call_count["count"] == 1, "第一次调用应触发 SQLite 读取"

        # 第二次调用:缓存命中,不触发 SQLite 读取
        result2 = manager.get_user_locale(60001)
        assert result2 == "en-US"
        assert call_count["count"] == 1, (
            "第二次调用应命中缓存,不触发 SQLite 读取"
        )

    def test_i18n_cache_ttl_expiry_triggers_reload(self, monkeypatch):
        """B9: TTL 过期后触发重新加载(缓存条目过期后下次访问重新读 SQLite)。"""
        import services.i18n as i18n_mod

        manager = i18n_mod.get_i18n_manager()
        call_count = {"count": 0}

        def counting_load(user_id):
            call_count["count"] += 1
            return "en-US"

        monkeypatch.setattr(manager, "_load_user_locale_from_sqlite", counting_load)

        # 第一次调用:缓存 miss,触发加载
        result1 = manager.get_user_locale(60002)
        assert result1 == "en-US"
        assert call_count["count"] == 1

        # 手动将缓存条目的过期时间设为过去(模拟 TTL 过期)
        import time as _time
        now = _time.time()
        # 获取当前缓存条目并修改过期时间
        cached = i18n_mod._user_locale_cache.get(60002)
        assert cached is not None, "第一次调用后缓存应有条目"
        # 替换为已过期的条目(expire_ts = now - 1,已过期)
        i18n_mod._user_locale_cache[60002] = (now - 1, "en-US")

        # 第二次调用:缓存过期,触发重新加载
        result2 = manager.get_user_locale(60002)
        assert result2 == "en-US"
        assert call_count["count"] == 2, (
            "缓存过期后应触发重新加载(SQLite 读取次数应为 2)"
        )

    def test_i18n_invalidate_cache_clears_entry(self):
        """B10: invalidate_user_locale_cache 清除指定条目(返回移除数)。"""
        import services.i18n as i18n_mod

        # 填充缓存
        i18n_mod._cache_user_locale(60003, "zh-CN")
        i18n_mod._cache_user_locale(60004, "en-US")
        assert 60003 in i18n_mod._user_locale_cache
        assert 60004 in i18n_mod._user_locale_cache

        # 失效指定用户
        removed = i18n_mod.invalidate_user_locale_cache(60003)
        assert removed == 1, "应移除 1 个条目"
        assert 60003 not in i18n_mod._user_locale_cache
        assert 60004 in i18n_mod._user_locale_cache, "其他用户条目应保留"

        # 失效全部
        removed_all = i18n_mod.invalidate_user_locale_cache(None)
        assert removed_all >= 1, "应移除剩余条目"
        assert len(i18n_mod._user_locale_cache) == 0, "缓存应已清空"

        # 失效不存在的条目(返回 0)
        removed_none = i18n_mod.invalidate_user_locale_cache(99999)
        assert removed_none == 0, "不存在的条目应返回 0"

    @pytest.mark.asyncio
    async def test_i18n_get_user_locale_async_works(self, monkeypatch):
        """B11: get_user_locale_async 异步版本正常工作(缓存命中 + miss 加载)。"""
        import services.i18n as i18n_mod

        manager = i18n_mod.get_i18n_manager()
        call_count = {"count": 0}

        def counting_load(user_id):
            call_count["count"] += 1
            return "en-US"

        monkeypatch.setattr(manager, "_load_user_locale_from_sqlite", counting_load)

        # 第一次调用:缓存 miss,在 executor 中加载
        result1 = await i18n_mod.get_user_locale_async(60005)
        assert result1 == "en-US"
        assert call_count["count"] == 1, "第一次调用应触发 SQLite 加载"

        # 第二次调用:缓存命中,直接返回(不触发 SQLite)
        result2 = await i18n_mod.get_user_locale_async(60005)
        assert result2 == "en-US"
        assert call_count["count"] == 1, (
            "第二次调用应命中缓存,不触发 SQLite 加载"
        )

    def test_i18n_set_user_locale_invalidates_cache(self, temp_i18n_db):
        """B12: set_user_locale 成功后主动失效缓存(下次访问重新加载新值)。

        使用真实临时 SQLite DB(含 users_local + dirty_outbox 表),
        验证 set_user_locale 写入成功后缓存被清除。
        """
        import services.i18n as i18n_mod

        # 填充缓存(模拟之前 get_user_locale 已缓存旧值)
        i18n_mod._cache_user_locale(50001, "zh-CN")
        assert 50001 in i18n_mod._user_locale_cache

        # 调用 set_user_locale 写入新 locale
        result = i18n_mod.set_user_locale(50001, "en-US")
        assert result is True, "set_user_locale 应返回 True(写入成功)"

        # 验证缓存已被失效
        assert 50001 not in i18n_mod._user_locale_cache, (
            "set_user_locale 成功后应主动失效该用户的 locale 缓存"
        )


# ════════════════════════════════════════════════════════════════
# C. 按钮安全(4 测试)
# ════════════════════════════════════════════════════════════════

class TestButtonSecurity:
    """C 节: services/button_security.py 的按钮安全(R51 P1-10)。

    验证:
    - generate_signed_callback 对高风险 action 记录 warning(不拒绝,向后兼容)
    - generate_signed_callback 对低风险 action 不记录 warning
    - sign_button_token_with_nonce + verify_button_token 端到端正常工作
    - verify_button_token 拒绝重放(nonce 已消费)
    """

    def test_generate_signed_callback_warns_for_high_risk(self):
        """C13: 高风险 action 通过 generate_signed_callback 签名时记录 warning。

        R51 P1-10: generate_signed_callback 为只读操作专用,
        高风险 action 应改用 sign_button_token_with_nonce。
        为保持向后兼容,不拒绝但记录 warning 日志。
        """
        from services.button_security import generate_signed_callback

        with patch("services.button_security.logger.warning") as mock_warn:
            result = generate_signed_callback(
                user_id=10001,
                action="ban",  # 高风险 action
                data="target_user:20001",
                ttl=3600,
            )

        # 应记录 warning
        assert mock_warn.called, (
            "高风险 action 通过 generate_signed_callback 签名时应记录 warning"
        )
        warn_msg = mock_warn.call_args[0][0]
        assert "R51 P1-10" in warn_msg, "warning 消息应包含 R51 P1-10 标识"
        assert "ban" in warn_msg, "warning 消息应包含 action 名称"
        assert "sign_button_token_with_nonce" in warn_msg, (
            "warning 消息应建议改用 sign_button_token_with_nonce"
        )
        # 仍应返回签名结果(不拒绝,向后兼容)
        assert isinstance(result, str)
        assert len(result) > 0, "应返回签名 callback_data(不拒绝)"

    def test_generate_signed_callback_no_warning_for_low_risk(self):
        """C14: 低风险 action 不记录 warning(只读操作允许使用旧 sync API)。"""
        from services.button_security import generate_signed_callback

        with patch("services.button_security.logger.warning") as mock_warn:
            result = generate_signed_callback(
                user_id=10001,
                action="view",  # 低风险 action(只读)
                data="file:ABC123",
                ttl=3600,
            )

        # 不应记录 warning
        assert not mock_warn.called, (
            "低风险 action 通过 generate_signed_callback 签名时不应记录 warning"
        )
        # 应正常返回签名结果
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_sign_and_verify_button_token_with_nonce(self, store):
        """C15: sign_button_token_with_nonce + verify_button_token 端到端。

        验证:
        1. sign_button_token_with_nonce 生成带持久化 nonce 的 token
        2. verify_button_token 验证签名 + 原子消费 nonce
        3. 验证通过后返回 (True, action, data)
        """
        from services.button_security import (
            sign_button_token_with_nonce,
            verify_button_token,
        )

        # 生成带 nonce 的 token(持久化到 callback_nonces 表)
        token = await sign_button_token_with_nonce(
            principal_id=10001,
            action="ban",
            payload="target_user:20001",
            ttl=3600,
        )

        # token 应为 6 段格式(含 nonce)
        parts = token.split(":")
        assert len(parts) >= 6, (
            f"token 应为 6 段格式(含 nonce),实际 {len(parts)} 段"
        )

        # 验证 token(签名校验 + nonce 原子消费)
        valid, action, data = await verify_button_token(token, 10001)

        assert valid is True, "首次验证应通过(签名匹配 + nonce 消费成功)"
        assert action == "ban", "应返回正确的 action"
        assert data == "target_user:20001", "应返回正确的 payload"

    @pytest.mark.asyncio
    async def test_verify_button_token_rejects_replay(self, store):
        """C16: 同一 token 第二次验证被拒(nonce 已消费,防重放)。

        R47 P1-a / R51 P1-10: nonce 原子消费(UPDATE WHERE consumed_at IS NULL),
        同一 nonce 只能消费一次,防止回调被并发重放。
        """
        from services.button_security import (
            sign_button_token_with_nonce,
            verify_button_token,
        )

        # 生成带 nonce 的 token
        token = await sign_button_token_with_nonce(
            principal_id=10002,
            action="delete_file",
            payload="FILE001",
            ttl=3600,
        )

        # 第一次验证:成功(消费 nonce)
        valid1, action1, _ = await verify_button_token(token, 10002)
        assert valid1 is True, "首次验证应成功"
        assert action1 == "delete_file"

        # 第二次验证:失败(nonce 已消费,拒绝重放)
        valid2, action2, _ = await verify_button_token(token, 10002)
        assert valid2 is False, (
            "第二次验证应失败(nonce 已消费,防重放)"
        )
        assert action2 == "", "验证失败时 action 应为空字符串"
