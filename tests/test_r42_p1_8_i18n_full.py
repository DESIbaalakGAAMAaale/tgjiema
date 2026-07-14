"""R42 P1-8: i18n 完整接入测试。

测试范围:
- services/i18n.py: R42 新增模块级函数
  * format_error_response(code, message_key, params, trace_id) -> dict
  * get_user_locale_sync(user_id) -> str
  * set_user_locale(user_id, locale) -> bool
  * format_plural(count, singular_key, plural_key, locale) -> str
- services/i18n.py: I18nManager 已有方法
  * format_message(key, locale, **kwargs) -> str
  * format_datetime(dt, locale, timezone) -> str
  * format_file_size(size_bytes, locale) -> str
- locales/zh-CN.json / en-US.json: key 一致性 + 非空翻译
- scripts/verify_i18n_keys.py: 校验脚本 exit code
- database/cache_store.py: users_local.locale 字段(已存在)

测试策略:
- 运行时调用 format_error_response / format_plural 等函数验证返回值
- 临时 SQLite DB 验证 get_user_locale_sync / set_user_locale
- 临时 locale 文件 + monkeypatch 验证 verify_i18n_keys.py exit code
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# mock telegram(避免 ImportError,conftest 已处理,此处兜底)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "services"
LOCALES_DIR = REPO_ROOT / "locales"
SCRIPTS_DIR = REPO_ROOT / "scripts"


# ── 辅助 fixture ──────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_i18n_singleton():
    """每个用例前重置 i18n 模块级单例,避免跨用例状态污染。"""
    import services.i18n as i18n_mod
    old = i18n_mod._i18n_manager
    i18n_mod._i18n_manager = None
    yield
    i18n_mod._i18n_manager = old


@pytest.fixture
def i18n_module():
    """导入 services.i18n 模块。"""
    import services.i18n as i18n_mod
    return i18n_mod


@pytest.fixture
def temp_db(tmp_path):
    """创建临时 SQLite DB,包含 users_local + dirty_outbox 表,返回 DB 路径。

    users_local 预置两个用户:
    - user_id=1001, locale=en-US
    - user_id=1002, locale=zh-CN
    """
    db_path = tmp_path / "test_cache.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE users_local (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            locale TEXT DEFAULT 'zh-CN',
            crdb_synced INTEGER DEFAULT 1
        )"""
    )
    conn.execute(
        """CREATE TABLE dirty_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            pk TEXT NOT NULL,
            version INTEGER DEFAULT 0,
            operation TEXT DEFAULT 'upsert',
            payload TEXT,
            created_at TEXT,
            processed INTEGER DEFAULT 0,
            local_only INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        "INSERT INTO users_local (user_id, username, locale) VALUES (?, ?, ?)",
        (1001, "testuser", "en-US"),
    )
    conn.execute(
        "INSERT INTO users_local (user_id, username, locale) VALUES (?, ?, ?)",
        (1002, "cn_user", "zh-CN"),
    )
    conn.commit()
    conn.close()
    return db_path


def _load_verify_module():
    """通过 importlib 加载 verify_i18n_keys.py 为独立模块实例。"""
    spec = importlib.util.spec_from_file_location(
        "_verify_i18n_keys_r42_test", SCRIPTS_DIR / "verify_i18n_keys.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ════════════════════════════════════════════════════════════════
# 1. format_error_response
# ════════════════════════════════════════════════════════════════


class TestFormatErrorResponse:
    """R42 P1-8: format_error_response 返回统一错误响应结构。"""

    def test_returns_correct_structure(self, i18n_module):
        """返回包含 code / message_key / params / trace_id 四个字段。"""
        result = i18n_module.format_error_response(
            code="QUOTA_EXCEEDED",
            message_key="errors.quota.decode.exceeded",
            params={"count": 5},
            trace_id="abc-123",
        )
        assert result["code"] == "QUOTA_EXCEEDED"
        assert result["message_key"] == "errors.quota.decode.exceeded"
        assert result["params"] == {"count": 5}
        assert result["trace_id"] == "abc-123"
        # 仅包含这 4 个字段
        assert set(result.keys()) == {"code", "message_key", "params", "trace_id"}

    def test_no_params_returns_empty_dict(self, i18n_module):
        """无 params 参数时 params={}。"""
        result = i18n_module.format_error_response(
            code="FILE_NOT_FOUND",
            message_key="errors.file.not_found",
        )
        assert result["params"] == {}

    def test_no_trace_id_returns_empty_string(self, i18n_module):
        """无 trace_id 参数时 trace_id=''。"""
        result = i18n_module.format_error_response(
            code="FILE_NOT_FOUND",
            message_key="errors.file.not_found",
        )
        assert result["trace_id"] == ""

    def test_params_none_explicitly_returns_empty(self, i18n_module):
        """显式传 params=None 时 params={}。"""
        result = i18n_module.format_error_response(
            code="X",
            message_key="errors.x",
            params=None,
            trace_id=None,
        )
        assert result["params"] == {}
        assert result["trace_id"] == ""


# ════════════════════════════════════════════════════════════════
# 2. get_user_locale_sync
# ════════════════════════════════════════════════════════════════


class TestGetUserLocaleSync:
    """R42 P1-8: get_user_locale_sync 从 users_local 读取 locale。"""

    def test_reads_locale_from_users_local(self, i18n_module, temp_db, monkeypatch):
        """从 users_local 表读取用户 locale(en-US)。"""
        monkeypatch.setattr("database.cache_store.DB_PATH", temp_db)
        locale = i18n_module.get_user_locale_sync(1001)
        assert locale == "en-US"

    def test_reads_zh_cn_user(self, i18n_module, temp_db, monkeypatch):
        """读取 zh-CN 用户的 locale。"""
        monkeypatch.setattr("database.cache_store.DB_PATH", temp_db)
        locale = i18n_module.get_user_locale_sync(1002)
        assert locale == "zh-CN"

    def test_fails_returns_default_zh_cn(self, i18n_module, monkeypatch, tmp_path):
        """DB 不存在 / 读取失败时返回默认 'zh-CN'。"""
        monkeypatch.setattr(
            "database.cache_store.DB_PATH", tmp_path / "nonexistent.db"
        )
        locale = i18n_module.get_user_locale_sync(9999)
        assert locale == "zh-CN"

    def test_user_not_found_returns_default(self, i18n_module, temp_db, monkeypatch):
        """用户不存在时返回默认 'zh-CN'。"""
        monkeypatch.setattr("database.cache_store.DB_PATH", temp_db)
        locale = i18n_module.get_user_locale_sync(8888)
        assert locale == "zh-CN"

    def test_zero_user_id_returns_default(self, i18n_module):
        """user_id=0 时返回默认 'zh-CN'。"""
        assert i18n_module.get_user_locale_sync(0) == "zh-CN"


# ════════════════════════════════════════════════════════════════
# 3. set_user_locale
# ════════════════════════════════════════════════════════════════


class TestSetUserLocale:
    """R42 P1-8: set_user_locale 写入用户 locale + dirty_outbox。"""

    def test_validates_locale_in_support_list(self, i18n_module, temp_db, monkeypatch):
        """set_user_locale 验证 locale 在支持列表中(zh-CN 成功)。"""
        monkeypatch.setattr("database.cache_store.DB_PATH", temp_db)
        result = i18n_module.set_user_locale(1001, "zh-CN")
        assert result is True

    def test_unsupported_locale_raises_value_error(
        self, i18n_module, temp_db, monkeypatch
    ):
        """不支持的 locale 抛出 ValueError。"""
        monkeypatch.setattr("database.cache_store.DB_PATH", temp_db)
        with pytest.raises(ValueError):
            i18n_module.set_user_locale(1001, "fr-FR")

    def test_writes_dirty_outbox(self, i18n_module, temp_db, monkeypatch):
        """set_user_locale 成功后 dirty_outbox 表应有一条 upsert 记录。"""
        monkeypatch.setattr("database.cache_store.DB_PATH", temp_db)
        result = i18n_module.set_user_locale(1001, "zh-CN")
        assert result is True
        # 验证 dirty_outbox 有记录
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.execute(
            "SELECT table_name, pk, operation, payload FROM dirty_outbox "
            "WHERE table_name = ?",
            ("users_local",),
        )
        rows = cursor.fetchall()
        conn.close()
        assert len(rows) >= 1
        row = rows[-1]
        assert row[0] == "users_local"
        assert row[1] == "1001"
        assert row[2] == "upsert"
        payload = json.loads(row[3])
        assert payload["user_id"] == 1001
        assert payload["locale"] == "zh-CN"

    def test_updates_users_local_locale(self, i18n_module, temp_db, monkeypatch):
        """set_user_locale 应更新 users_local.locale 字段。"""
        monkeypatch.setattr("database.cache_store.DB_PATH", temp_db)
        # 1001 原本是 en-US,改为 zh-CN
        result = i18n_module.set_user_locale(1001, "zh-CN")
        assert result is True
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.execute(
            "SELECT locale FROM users_local WHERE user_id = ?", (1001,)
        )
        row = cursor.fetchone()
        conn.close()
        assert row[0] == "zh-CN"

    def test_db_not_exists_returns_false(self, i18n_module, monkeypatch, tmp_path):
        """DB 不存在时返回 False。"""
        monkeypatch.setattr(
            "database.cache_store.DB_PATH", tmp_path / "nonexistent.db"
        )
        result = i18n_module.set_user_locale(1001, "zh-CN")
        assert result is False

    def test_zero_user_id_returns_false(self, i18n_module):
        """user_id=0 时返回 False。"""
        assert i18n_module.set_user_locale(0, "zh-CN") is False


# ════════════════════════════════════════════════════════════════
# 4. format_plural
# ════════════════════════════════════════════════════════════════


class TestFormatPlural:
    """R42 P1-8: format_plural 根据语言规则返回单复数形式。"""

    def test_chinese_no_plural_distinction(self, i18n_module):
        """中文不区分单复数,始终用 singular_key。"""
        # count=1
        result = i18n_module.format_plural(
            1, "bot.file_count_singular", "bot.file_count_plural", locale="zh-CN"
        )
        assert result == "1 个文件"
        # count=5
        result = i18n_module.format_plural(
            5, "bot.file_count_singular", "bot.file_count_plural", locale="zh-CN"
        )
        assert result == "5 个文件"

    def test_english_singular_for_count_one(self, i18n_module):
        """英文 count=1 用 singular_key。"""
        result = i18n_module.format_plural(
            1, "bot.file_count_singular", "bot.file_count_plural", locale="en-US"
        )
        assert result == "1 file"

    def test_english_plural_for_count_greater_than_one(self, i18n_module):
        """英文 count>1 用 plural_key。"""
        result = i18n_module.format_plural(
            5, "bot.file_count_singular", "bot.file_count_plural", locale="en-US"
        )
        assert result == "5 files"

    def test_english_count_zero_uses_plural(self, i18n_module):
        """英文 count=0 用 plural_key(英文 '0 files' 习惯)。"""
        result = i18n_module.format_plural(
            0, "bot.file_count_singular", "bot.file_count_plural", locale="en-US"
        )
        assert result == "0 files"

    def test_default_locale_is_zh_cn(self, i18n_module):
        """不传 locale 时默认 zh-CN(不区分单复数)。"""
        result = i18n_module.format_plural(
            3, "bot.file_count_singular", "bot.file_count_plural"
        )
        assert result == "3 个文件"


# ════════════════════════════════════════════════════════════════
# 5. format_message
# ════════════════════════════════════════════════════════════════


class TestFormatMessage:
    """R42 P1-8: format_message 显式格式化接口。"""

    def test_missing_key_returns_key_itself(self, i18n_module):
        """R44 6.2: 缺失 key 时返回安全通用文案(不暴露内部 key)。

        旧行为: 返回 key 本身(暴露内部 key 给用户)
        新行为(R44 6.2): 按 key 前缀返回安全通用文案 + 增加 missing_key 计数
        """
        manager = i18n_module.get_i18n_manager()
        # 重置计数器,验证本次调用会触发计数
        manager.reset_missing_key_count()
        result = manager.format_message(
            "nonexistent.key.xyz", locale="zh-CN"
        )
        # R44 6.2: 不再返回 key 本身,而是返回安全通用文案
        assert result != "nonexistent.key.xyz"
        # 应返回非空的安全文案
        assert isinstance(result, str)
        assert len(result) > 0
        # missing_key 计数应已递增
        assert manager.get_missing_key_count() >= 1

    def test_locale_not_exist_fallback_to_en_us(self, i18n_module):
        """locale 不存在时 fallback 到 en-US。"""
        # bot.upload_success 在 en-US 是 "Upload successful, file code: {code}"
        result = i18n_module.get_i18n_manager().format_message(
            "bot.upload_success", locale="xx-XX", code="ABC123"
        )
        assert result == "Upload successful, file code: ABC123"

    def test_param_replacement_correct(self, i18n_module):
        """参数替换正确({code} 占位符)。"""
        result = i18n_module.get_i18n_manager().format_message(
            "bot.upload_success", locale="en-US", code="XYZ789"
        )
        assert result == "Upload successful, file code: XYZ789"

    def test_param_replacement_zh_cn(self, i18n_module):
        """中文参数替换正确。"""
        result = i18n_module.get_i18n_manager().format_message(
            "bot.upload_success", locale="zh-CN", code="码001"
        )
        assert result == "上传成功,文件码: 码001"


# ════════════════════════════════════════════════════════════════
# 6. format_datetime
# ════════════════════════════════════════════════════════════════


class TestFormatDatetime:
    """R42 P1-8: format_datetime 按用户时区格式化。"""

    def test_formats_by_user_timezone(self, i18n_module):
        """按用户时区格式化(UTC -> Asia/Shanghai +8)。

        tzdata 可用时: UTC 06:30 -> 14:30
        tzdata 不可用时: fallback 到 UTC(06:30)
        """
        dt = datetime.datetime(
            2024, 1, 15, 6, 30, tzinfo=datetime.timezone.utc
        )
        result = i18n_module.get_i18n_manager().format_datetime(
            dt, locale="zh-CN", timezone="Asia/Shanghai"
        )
        assert "2024年1月15日" in result
        # 检测 tzdata 是否可用
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("Asia/Shanghai")
            tzdata_ok = True
        except Exception:
            tzdata_ok = False
        if tzdata_ok:
            assert "14:30" in result
        else:
            # tzdata 不可用时 fallback 到 UTC
            assert "06:30" in result

    def test_formats_utc_timezone(self, i18n_module):
        """UTC 时区格式化(不依赖 tzdata,跨平台稳定)。"""
        dt = datetime.datetime(
            2024, 1, 15, 14, 30, tzinfo=datetime.timezone.utc
        )
        result = i18n_module.get_i18n_manager().format_datetime(
            dt, locale="zh-CN", timezone="UTC"
        )
        assert "2024年1月15日" in result
        assert "14:30" in result

    def test_en_us_format(self, i18n_module):
        """英文日期格式化。"""
        dt = datetime.datetime(
            2024, 1, 15, 14, 30, tzinfo=datetime.timezone.utc
        )
        result = i18n_module.get_i18n_manager().format_datetime(
            dt, locale="en-US", timezone="UTC"
        )
        assert "Jan" in result
        assert "2024" in result


# ════════════════════════════════════════════════════════════════
# 7. format_file_size
# ════════════════════════════════════════════════════════════════


class TestFormatFileSize:
    """R42 P1-8: format_file_size 文件大小格式化(B/KB/MB/GB)。"""

    def test_bytes(self, i18n_module):
        """小于 1024 字节显示 B。"""
        result = i18n_module.get_i18n_manager().format_file_size(500, locale="zh-CN")
        assert result == "500 B"

    def test_kilobytes(self, i18n_module):
        """1024 字节 = 1.0 KB。"""
        result = i18n_module.get_i18n_manager().format_file_size(1024, locale="zh-CN")
        assert result == "1.0 KB"

    def test_megabytes(self, i18n_module):
        """1048576 字节 = 1.0 MB。"""
        result = i18n_module.get_i18n_manager().format_file_size(1048576, locale="zh-CN")
        assert result == "1.0 MB"

    def test_gigabytes(self, i18n_module):
        """1073741824 字节 = 1.0 GB。"""
        result = i18n_module.get_i18n_manager().format_file_size(
            1073741824, locale="zh-CN"
        )
        assert result == "1.0 GB"


# ════════════════════════════════════════════════════════════════
# 8. verify_i18n_keys.py exit code
# ════════════════════════════════════════════════════════════════


class TestVerifyI18nKeys:
    """R42 P1-8: scripts/verify_i18n_keys.py 校验脚本 exit code。"""

    def test_exits_0_when_keys_consistent(self, tmp_path, monkeypatch):
        """key 一致 + 非空翻译时 exit 0。"""
        locales_dir = tmp_path / "locales"
        locales_dir.mkdir()
        (locales_dir / "zh-CN.json").write_text(
            json.dumps(
                {"meta": {"locale": "zh-CN"}, "errors": {"x": "错误"}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (locales_dir / "en-US.json").write_text(
            json.dumps(
                {"meta": {"locale": "en-US"}, "errors": {"x": "Error"}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        mod = _load_verify_module()
        monkeypatch.setattr(mod, "LOCALES_DIR", locales_dir)
        # R47 P1-c: step 7 校验 ErrorCodes message_key,测试 fixture 无完整 key,
        # mock 为空列表避免干扰基础 key 一致性校验
        monkeypatch.setattr(mod, "_verify_error_code_message_keys", lambda zh, en: [])
        assert mod.verify() == 0

    def test_exits_1_when_zh_cn_missing_key(self, tmp_path, monkeypatch):
        """zh-CN 缺失 key 时 exit 1。"""
        locales_dir = tmp_path / "locales"
        locales_dir.mkdir()
        (locales_dir / "zh-CN.json").write_text(
            json.dumps({"meta": {}, "errors": {}}, ensure_ascii=False),
            encoding="utf-8",
        )
        (locales_dir / "en-US.json").write_text(
            json.dumps(
                {"meta": {}, "errors": {"x": "Error"}}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        mod = _load_verify_module()
        monkeypatch.setattr(mod, "LOCALES_DIR", locales_dir)
        assert mod.verify() == 1

    def test_exits_1_when_en_us_missing_key(self, tmp_path, monkeypatch):
        """en-US 缺失 key 时 exit 1。"""
        locales_dir = tmp_path / "locales"
        locales_dir.mkdir()
        (locales_dir / "zh-CN.json").write_text(
            json.dumps(
                {"meta": {}, "errors": {"x": "错误"}}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        (locales_dir / "en-US.json").write_text(
            json.dumps({"meta": {}, "errors": {}}, ensure_ascii=False),
            encoding="utf-8",
        )
        mod = _load_verify_module()
        monkeypatch.setattr(mod, "LOCALES_DIR", locales_dir)
        assert mod.verify() == 1

    def test_exits_1_when_translation_empty(self, tmp_path, monkeypatch):
        """翻译为空时 exit 1。"""
        locales_dir = tmp_path / "locales"
        locales_dir.mkdir()
        (locales_dir / "zh-CN.json").write_text(
            json.dumps({"meta": {}, "errors": {"x": ""}}, ensure_ascii=False),
            encoding="utf-8",
        )
        (locales_dir / "en-US.json").write_text(
            json.dumps({"meta": {}, "errors": {"x": ""}}, ensure_ascii=False),
            encoding="utf-8",
        )
        mod = _load_verify_module()
        monkeypatch.setattr(mod, "LOCALES_DIR", locales_dir)
        assert mod.verify() == 1
