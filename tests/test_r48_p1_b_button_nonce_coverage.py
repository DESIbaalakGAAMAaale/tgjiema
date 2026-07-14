"""R48 P1-b 终审整改测试:按钮 nonce API 全覆盖扫描 + resource_version 绑定 + 定时清理。

测试范围:
1. scripts/check_button_nonce_coverage.py (静态扫描器)
   - 高风险 action 使用旧 sync API 被检测(violation)
   - 高风险 action 使用新 async API 通过(no violation)
   - 低风险 action 使用旧 sync API 允许(no violation,向后兼容)
   - action 为变量时跳过(无法静态判定)
   - 白名单文件跳过(services/button_security.py / tests/ / scripts/)
   - 整个代码库扫描通过(无违规)

2. services/button_security.py (resource_version 绑定)
   - resource_version 匹配时签名验证通过
   - resource_version 不匹配时签名验证拒绝
   - resource_version 空值向后兼容
   - resource_version 不出现在 callback_data 中(仅参与签名)

3. services/button_security.py (validate_production_config)
   - production 缺 BOT_TOKEN 抛 RuntimeError(fail-closed)
   - development 不抛异常(宽松)
   - validate_production_config 与 _check_production_secret 行为一致

4. database/cache_store.py (callback_nonce_cleanup)
   - 清理过期未消费的 nonce
   - 清理已消费超过保留期的 nonce
   - None 参数时不清理
   - 幂等性(重复调用无副作用)

测试策略:
- 扫描器测试:importlib 加载脚本 + 临时 repo 结构 + monkeypatch REPO_ROOT
- resource_version 测试:固定 nonce 确保签名可重现
- cleanup 测试:真实 SQLite 临时文件数据库(隔离于生产 cache_store.db)
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from services.error_codes import AppError, ErrorCodes

# mock telegram(避免 ImportError,conftest 已处理,此处兜底)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

# ── 模块级 skip 检查:cache_store 必须是真实类(非 conftest 降级 MagicMock)──
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


# ── 辅助函数 ──────────────────────────────────────────


def _load_scanner_module():
    """通过 importlib 加载 check_button_nonce_coverage.py 为独立模块实例。

    每次返回全新的模块对象,避免跨用例状态污染。
    """
    spec = importlib.util.spec_from_file_location(
        "_check_button_nonce_coverage_r48_test",
        SCRIPTS_DIR / "check_button_nonce_coverage.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_check(tmp_path, files):
    """创建临时 repo 结构并运行 scanner.check()。

    Args:
        tmp_path: pytest tmp_path fixture(Path)
        files: dict, {相对路径: 文件内容}
            如 {"bots/bad.py": "generate_signed_callback(1, 'ban', 'x')\\n"}

    Returns:
        (exit_code, violations, info)
        exit_code: 0=无违规,1=有高风险 action 违规
        violations: 违规列表
        info: 所有扫描到的 API 调用点(用于报告)
    """
    fake_root = tmp_path / "fake_repo"
    fake_root.mkdir()
    for rel_path, content in files.items():
        full_path = fake_root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")

    mod = _load_scanner_module()
    # 替换 REPO_ROOT 为临时目录,使 check() 仅扫描临时 repo
    mod.REPO_ROOT = fake_root
    return mod.check()


# ── Fixtures ──────────────────────────────────────────


@pytest_asyncio.fixture
async def store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离于生产 cache_store.db)。

    策略:
    1. 临时目录下的 test_r48_nonce_cleanup.db
    2. 直接替换 database.cache_store.DB_PATH 指向临时路径
    3. 替换 database.cache_store.get_cache_store 返回测试 store
    4. 结束后恢复 + close + shutil.rmtree
    """
    tmpdir = tempfile.mkdtemp(prefix="r48_p1_b_test_")
    db_path = Path(tmpdir) / "test_r48_nonce_cleanup.db"
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


@pytest.fixture(autouse=True)
def setup_bot_token(monkeypatch):
    """为 button_security 提供固定 BOT_TOKEN(避免 MagicMock 导致 HMAC 失败)。

    conftest 注入的 settings 是 MagicMock,ADMIN_BOT_TOKEN 属性也是 MagicMock,
    调用 .encode() 会返回 MagicMock 导致 hmac.new() 抛错。
    此处将其设为固定字符串,确保 _sign() 可正常工作。
    """
    import config
    monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "r48_test_admin_bot_token")
    monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "r48_test_sender_bot_token")
    # 默认 development 环境(避免触发 production fail-closed)
    monkeypatch.setattr(config.settings, "ENVIRONMENT", "development")


# ════════════════════════════════════════════════════════════════
# 1. 扫描器测试:高风险 action 使用旧 sync API 被检测
# ════════════════════════════════════════════════════════════════


class TestScannerHighRiskOldApiDetected:
    """R48 P1-b: 高风险 action 使用旧 sync API 必须被检测为违规。"""

    def test_high_risk_generate_signed_callback_detected(self, tmp_path):
        """高风险 action(ban)使用 generate_signed_callback → 检测为违规。"""
        files = {
            "bots/bad_bot.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(user_id=1, action='ban', data='x')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 1
        assert len(violations) == 1
        assert violations[0]["action"] == "ban"
        assert violations[0]["func"] == "generate_signed_callback"

    def test_high_risk_takedown_generate_detected(self, tmp_path):
        """高风险 action(takedown)使用 generate_signed_callback → 检测为违规。"""
        files = {
            "bots/bad_takedown.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(user_id=1, action='takedown', data='file_123')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 1
        assert len(violations) == 1
        assert violations[0]["action"] == "takedown"

    def test_high_risk_purge_generate_detected(self, tmp_path):
        """高风险 action(purge)使用 generate_signed_callback → 检测为违规。"""
        files = {
            "bots/bad_purge.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(1, 'purge', 'x')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 1
        assert len(violations) == 1
        assert violations[0]["action"] == "purge"

    def test_high_risk_admin_grant_keyword_arg_detected(self, tmp_path):
        """高风险 action(admin_grant)用关键字参数 → 检测为违规。"""
        files = {
            "bots/bad_admin.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(user_id=1, action='admin_grant', data='role')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 1
        assert len(violations) == 1
        assert violations[0]["action"] == "admin_grant"

    def test_verify_signed_callback_not_flagged(self, tmp_path):
        """verify_signed_callback 的 action 在 callback_data 内部,无法静态提取,不检测。

        扫描器仅对 generate_signed_callback / sign_button_token_with_nonce 提取 action,
        verify_signed_callback / verify_button_token 的 action 在 callback_data 字符串内部,
        无法在调用点静态判定,故不检测(避免误报)。
        """
        files = {
            "bots/verify_bot.py": (
                "from services.button_security import verify_signed_callback\n"
                "verify_signed_callback('1:ban:x:9999:nonce:sig', 1)\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        # verify_signed_callback 的 action 无法静态提取,不算违规
        assert exit_code == 0
        assert violations == []


# ════════════════════════════════════════════════════════════════
# 2. 扫描器测试:高风险 action 使用新 async API 通过
# ════════════════════════════════════════════════════════════════


class TestScannerHighRiskNewApiPassed:
    """R48 P1-b: 高风险 action 使用新 async API 不应被检测为违规。"""

    def test_high_risk_sign_button_token_with_nonce_passed(self, tmp_path):
        """高风险 action(ban)使用 sign_button_token_with_nonce → 无违规。"""
        files = {
            "bots/good_bot.py": (
                "from services.button_security import sign_button_token_with_nonce\n"
                "await sign_button_token_with_nonce(principal_id=1, action='ban', payload='x')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_high_risk_takedown_sign_passed(self, tmp_path):
        """高风险 action(takedown)使用 sign_button_token_with_nonce → 无违规。"""
        files = {
            "bots/good_takedown.py": (
                "from services.button_security import sign_button_token_with_nonce\n"
                "await sign_button_token_with_nonce(1, 'takedown', 'file_abc')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_verify_button_token_passed(self, tmp_path):
        """verify_button_token(async)不检测 action(在 callback_data 内部)。"""
        files = {
            "bots/good_verify.py": (
                "from services.button_security import verify_button_token\n"
                "await verify_button_token('1:ban:x:9999:nonce:sig', 1)\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []


# ════════════════════════════════════════════════════════════════
# 3. 扫描器测试:低风险 action 使用旧 sync API 允许
# ════════════════════════════════════════════════════════════════


class TestScannerLowRiskOldApiAllowed:
    """R48 P1-b: 低风险 action 使用旧 sync API 应被允许(向后兼容)。"""

    def test_low_risk_cancel_allowed(self, tmp_path):
        """低风险 action(cancel)使用 generate_signed_callback → 无违规。"""
        files = {
            "bots/low_risk.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(user_id=1, action='cancel', data='x')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_unknown_action_confirm_allowed(self, tmp_path):
        """未知 action(confirm)使用 generate_signed_callback → 无违规。"""
        files = {
            "bots/unknown.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(user_id=1, action='confirm', data='x')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_low_risk_view_allowed(self, tmp_path):
        """低风险 action(view)使用 generate_signed_callback → 无违规。"""
        files = {
            "bots/view.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(1, 'view', 'page_1')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []


# ════════════════════════════════════════════════════════════════
# 4. 扫描器测试:action 为变量时跳过
# ════════════════════════════════════════════════════════════════


class TestScannerVariableActionSkipped:
    """R48 P1-b: action 为变量(非字符串字面量)时跳过,不检测。"""

    def test_variable_action_not_flagged(self, tmp_path):
        """action 为变量 → 无法判定是否高风险,不检测(避免误报)。"""
        files = {
            "bots/variable.py": (
                "from services.button_security import generate_signed_callback\n"
                "action_var = 'ban'\n"
                "generate_signed_callback(user_id=1, action=action_var, data='x')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_function_call_action_not_flagged(self, tmp_path):
        """action 为函数调用结果 → 无法判定,不检测。"""
        files = {
            "bots/func_action.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(user_id=1, action=get_action(), data='x')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []


# ════════════════════════════════════════════════════════════════
# 5. 扫描器测试:白名单文件跳过
# ════════════════════════════════════════════════════════════════


class TestScannerWhitelistSkipped:
    """R48 P1-b: 白名单文件跳过扫描(API 定义文件 / 测试 / 脚本)。"""

    def test_button_security_py_skipped(self, tmp_path):
        """services/button_security.py 内部调用跳过(API 定义文件)。"""
        files = {
            "services/button_security.py": (
                "# API 定义文件\n"
                "def generate_signed_callback(user_id, action, data):\n"
                "    pass\n"
                "generate_signed_callback(1, 'ban', 'x')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_tests_directory_skipped(self, tmp_path):
        """tests/ 目录下的文件跳过。"""
        files = {
            "tests/test_bad.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(user_id=1, action='ban', data='x')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_scripts_directory_skipped(self, tmp_path):
        """scripts/ 目录下的文件跳过。"""
        files = {
            "scripts/check.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(user_id=1, action='ban', data='x')\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []


# ════════════════════════════════════════════════════════════════
# 6. 扫描器测试:整个代码库扫描通过
# ════════════════════════════════════════════════════════════════


class TestScannerFullCodebasePass:
    """R48 P1-b: 整个代码库扫描通过(无高风险 action 使用旧 sync API)。"""

    def test_full_codebase_no_violations(self):
        """扫描真实代码库,验证无违规(集成测试)。"""
        mod = _load_scanner_module()
        exit_code, violations, info = mod.check()
        assert exit_code == 0, f"代码库中存在违规: {violations}"
        assert violations == [], f"违规列表应为空,实际: {violations}"


# ════════════════════════════════════════════════════════════════
# 7. resource_version 绑定测试
# ════════════════════════════════════════════════════════════════


class TestResourceVersionBinding:
    """R48 P1-b: resource_version 绑定,防止旧按钮操作已更新资源。

    resource_version 参与签名 payload 计算但不出现在 callback_data 字符串中,
    验证方必须传入相同的 resource_version 才能通过签名校验。
    """

    def test_resource_version_match_accepted(self):
        """签名时绑定 rv='v1',验证时传入 rv='v1' → 通过。"""
        from services.button_security import generate_signed_callback, verify_signed_callback
        token = generate_signed_callback(
            user_id=12345, action="cancel", data="x", ttl=3600,
            nonce="fixednonce123", resource_version="v1",
        )
        valid, action, data = verify_signed_callback(token, 12345, resource_version="v1")
        assert valid is True
        assert action == "cancel"
        assert data == "x"

    def test_resource_version_mismatch_rejected(self):
        """签名时绑定 rv='v1',验证时传入 rv='v2' → 拒绝(签名不匹配)。"""
        from services.button_security import generate_signed_callback, verify_signed_callback
        token = generate_signed_callback(
            user_id=12345, action="cancel", data="x", ttl=3600,
            nonce="fixednonce123", resource_version="v1",
        )
        valid, _, _ = verify_signed_callback(token, 12345, resource_version="v2")
        assert valid is False, "resource_version 不匹配必须拒绝"

    def test_binding_with_empty_verify_rejected(self):
        """签名时绑定 rv='v1',验证时不传 rv → 拒绝(签名 payload 不同)。"""
        from services.button_security import generate_signed_callback, verify_signed_callback
        token = generate_signed_callback(
            user_id=12345, action="cancel", data="x", ttl=3600,
            nonce="fixednonce123", resource_version="v1",
        )
        valid, _, _ = verify_signed_callback(token, 12345, resource_version="")
        assert valid is False, "签名时绑定了 rv,验证时不传必须拒绝"

    def test_no_binding_backward_compat(self):
        """签名时不传 rv,验证时也不传 rv → 通过(向后兼容)。"""
        from services.button_security import generate_signed_callback, verify_signed_callback
        token = generate_signed_callback(
            user_id=12345, action="cancel", data="x", ttl=3600,
            nonce="fixednonce123",  # 不传 resource_version
        )
        valid, action, data = verify_signed_callback(token, 12345)  # 不传 resource_version
        assert valid is True
        assert action == "cancel"
        assert data == "x"

    def test_no_binding_verify_with_rv_rejected(self):
        """签名时不传 rv,验证时传入 rv → 拒绝(签名 payload 不同)。"""
        from services.button_security import generate_signed_callback, verify_signed_callback
        token = generate_signed_callback(
            user_id=12345, action="cancel", data="x", ttl=3600,
            nonce="fixednonce123",  # 不传 resource_version
        )
        valid, _, _ = verify_signed_callback(token, 12345, resource_version="v1")
        assert valid is False, "签名时未绑定 rv,验证时传入 rv 必须拒绝"

    def test_resource_version_not_in_callback_data(self):
        """resource_version 不出现在 callback_data 字符串中(仅参与签名)。"""
        from services.button_security import generate_signed_callback
        token = generate_signed_callback(
            user_id=12345, action="cancel", data="x", ttl=3600,
            nonce="fixednonce123", resource_version="secret_version_42",
        )
        # callback_data 格式: {user_id}:{action}:{data}:{expire_ts}:{nonce}:{signature}
        parts = token.split(":")
        assert len(parts) == 6, "callback_data 必须为 6 段格式"
        # resource_version 不应出现在任何一段中
        for part in parts:
            assert "secret_version_42" not in part, (
                "resource_version 不应出现在 callback_data 中"
            )

    def test_different_resource_versions_produce_different_signatures(self):
        """不同的 resource_version 产生不同的签名(验证 rv 确实参与签名)。"""
        from services.button_security import generate_signed_callback
        token_v1 = generate_signed_callback(
            user_id=12345, action="cancel", data="x", ttl=3600,
            nonce="fixednonce123", resource_version="v1",
        )
        token_v2 = generate_signed_callback(
            user_id=12345, action="cancel", data="x", ttl=3600,
            nonce="fixednonce123", resource_version="v2",
        )
        sig_v1 = token_v1.split(":")[-1]
        sig_v2 = token_v2.split(":")[-1]
        assert sig_v1 != sig_v2, "不同 resource_version 必须产生不同签名"

    def test_resource_version_with_high_risk_action(self):
        """高风险 action + resource_version 绑定(低风险 action 路径测试)。

        注:高风险 action 应使用 sign_button_token_with_nonce(async),
        但 generate_signed_callback 仍支持 resource_version 参数(低风险场景向后兼容)。
        此处用低风险 action(cancel)测试 resource_version 绑定功能。
        """
        from services.button_security import generate_signed_callback, verify_signed_callback
        token = generate_signed_callback(
            user_id=99999, action="cancel", data="item_42", ttl=3600,
            nonce="fixednonce456", resource_version="rev_2026_07_14",
        )
        # 正确 rv → 通过
        valid, _, _ = verify_signed_callback(token, 99999, resource_version="rev_2026_07_14")
        assert valid is True
        # 错误 rv → 拒绝
        valid, _, _ = verify_signed_callback(token, 99999, resource_version="rev_2026_07_15")
        assert valid is False


# ════════════════════════════════════════════════════════════════
# 8. validate_production_config 测试
# ════════════════════════════════════════════════════════════════


class TestValidateProductionConfig:
    """R48 P1-b: validate_production_config() 供 Bot 启动时显式调用。

    与 _check_production_secret 的区别:
    - _check_production_secret 在模块导入时自动调用(可能因 conftest MagicMock 不触发)
    - validate_production_config 供 Bot 启动时显式调用,确保每次启动都检查
    - 内部调用 _check_production_secret,行为一致
    """

    def test_production_missing_both_tokens_raises(self, monkeypatch):
        """production + ADMIN_BOT_TOKEN 和 SENDER_BOT_TOKEN 均空 → AppError(PRODUCTION_BOT_TOKEN_MISSING)。"""
        import config
        from services.button_security import validate_production_config

        monkeypatch.setattr(config.settings, "ENVIRONMENT", "production")
        monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "")
        monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "")
        with pytest.raises(AppError) as exc_info:
            validate_production_config()
        assert exc_info.value.envelope.code == ErrorCodes.PRODUCTION_BOT_TOKEN_MISSING

    def test_production_with_admin_token_passes(self, monkeypatch):
        """production + 仅 ADMIN_BOT_TOKEN 配置 → 通过。"""
        import config
        from services.button_security import validate_production_config

        monkeypatch.setattr(config.settings, "ENVIRONMENT", "production")
        monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "admin_prod_token")
        monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "")
        validate_production_config()  # 不应抛异常

    def test_production_with_sender_token_passes(self, monkeypatch):
        """production + 仅 SENDER_BOT_TOKEN 配置 → 通过。"""
        import config
        from services.button_security import validate_production_config

        monkeypatch.setattr(config.settings, "ENVIRONMENT", "production")
        monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "")
        monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "sender_prod_token")
        validate_production_config()

    def test_development_missing_tokens_passes(self, monkeypatch):
        """development + 两个 token 均空 → 通过(宽松,允许 default_secret)。"""
        import config
        from services.button_security import validate_production_config

        monkeypatch.setattr(config.settings, "ENVIRONMENT", "development")
        monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "")
        monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "")
        validate_production_config()

    def test_validate_production_config_same_behavior_as_check(self, monkeypatch):
        """validate_production_config 与 _check_production_secret 行为一致。"""
        import config
        from services.button_security import (
            validate_production_config,
            _check_production_secret,
        )

        monkeypatch.setattr(config.settings, "ENVIRONMENT", "production")
        monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "")
        monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "")

        # 两者都应抛 AppError(PRODUCTION_BOT_TOKEN_MISSING)
        with pytest.raises(AppError) as exc_info:
            _check_production_secret()
        assert exc_info.value.envelope.code == ErrorCodes.PRODUCTION_BOT_TOKEN_MISSING

        with pytest.raises(AppError) as exc_info:
            validate_production_config()
        assert exc_info.value.envelope.code == ErrorCodes.PRODUCTION_BOT_TOKEN_MISSING


# ════════════════════════════════════════════════════════════════
# 9. callback_nonce_cleanup 测试
# ════════════════════════════════════════════════════════════════


class TestCallbackNonceCleanup:
    """R48 P1-b: callback_nonce_cleanup 清理过期/已消费 nonce。

    清理策略:
    1. 删除 expires_at < expired_before 的记录(已过期但未消费)
    2. 删除 consumed_at < consumed_before 的记录(已消费超过保留期)
    """

    @pytest.mark.asyncio
    async def test_cleanup_expired_nonces(self, store):
        """清理过期未消费的 nonce。"""
        past = (_dt.datetime.now() - _dt.timedelta(hours=1)).isoformat()
        future = (_dt.datetime.now() + _dt.timedelta(hours=1)).isoformat()
        await store.callback_nonce_create(
            nonce="expired_001", principal_id=1, action="test", expires_at=past
        )
        await store.callback_nonce_create(
            nonce="valid_001", principal_id=1, action="test", expires_at=future
        )
        now = _dt.datetime.now().isoformat()
        result = await store.callback_nonce_cleanup(expired_before=now)
        assert result["deleted_expired"] == 1
        assert result["deleted_consumed"] == 0
        assert await store.callback_nonce_exists("expired_001") is False
        assert await store.callback_nonce_exists("valid_001") is True

    @pytest.mark.asyncio
    async def test_cleanup_consumed_nonces(self, store):
        """清理已消费超过保留期的 nonce。"""
        future = (_dt.datetime.now() + _dt.timedelta(hours=1)).isoformat()
        await store.callback_nonce_create(
            nonce="consumed_001", principal_id=1, action="test", expires_at=future
        )
        await store.callback_nonce_create(
            nonce="unconsumed_001", principal_id=1, action="test", expires_at=future
        )
        # 消费 consumed_001(设置 consumed_at = now)
        await store.callback_nonce_consume("consumed_001")
        # cutoff 设为未来 1 小时(所有已消费的都会被清理)
        future_cutoff = (_dt.datetime.now() + _dt.timedelta(hours=1)).isoformat()
        result = await store.callback_nonce_cleanup(consumed_before=future_cutoff)
        assert result["deleted_expired"] == 0
        assert result["deleted_consumed"] == 1
        assert await store.callback_nonce_exists("consumed_001") is False
        assert await store.callback_nonce_exists("unconsumed_001") is True

    @pytest.mark.asyncio
    async def test_cleanup_consumed_recent_kept(self, store):
        """已消费但 consumed_at 在 cutoff 之后 → 保留(不清理)。"""
        future = (_dt.datetime.now() + _dt.timedelta(hours=1)).isoformat()
        await store.callback_nonce_create(
            nonce="recent_consumed", principal_id=1, action="test", expires_at=future
        )
        await store.callback_nonce_consume("recent_consumed")
        # cutoff 设为过去 1 小时(consumed_at = now > cutoff → 不清理)
        past_cutoff = (_dt.datetime.now() - _dt.timedelta(hours=1)).isoformat()
        result = await store.callback_nonce_cleanup(consumed_before=past_cutoff)
        assert result["deleted_consumed"] == 0
        assert await store.callback_nonce_exists("recent_consumed") is True

    @pytest.mark.asyncio
    async def test_cleanup_none_params_no_deletion(self, store):
        """expired_before=None + consumed_before=None → 不清理。"""
        future = (_dt.datetime.now() + _dt.timedelta(hours=1)).isoformat()
        await store.callback_nonce_create(
            nonce="keep_001", principal_id=1, action="test", expires_at=future
        )
        result = await store.callback_nonce_cleanup()
        assert result["deleted_expired"] == 0
        assert result["deleted_consumed"] == 0
        assert await store.callback_nonce_exists("keep_001") is True

    @pytest.mark.asyncio
    async def test_cleanup_idempotent(self, store):
        """重复调用 cleanup 不会删除更多记录(幂等)。"""
        past = (_dt.datetime.now() - _dt.timedelta(hours=1)).isoformat()
        await store.callback_nonce_create(
            nonce="expired_002", principal_id=1, action="test", expires_at=past
        )
        now = _dt.datetime.now().isoformat()
        result1 = await store.callback_nonce_cleanup(expired_before=now)
        result2 = await store.callback_nonce_cleanup(expired_before=now)
        assert result1["deleted_expired"] == 1
        assert result2["deleted_expired"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_both_params(self, store):
        """同时清理过期和已消费的 nonce。"""
        past = (_dt.datetime.now() - _dt.timedelta(hours=2)).isoformat()
        future = (_dt.datetime.now() + _dt.timedelta(hours=1)).isoformat()
        # 过期未消费
        await store.callback_nonce_create(
            nonce="expired_003", principal_id=1, action="test", expires_at=past
        )
        # 未过期但已消费
        await store.callback_nonce_create(
            nonce="consumed_003", principal_id=1, action="test", expires_at=future
        )
        await store.callback_nonce_consume("consumed_003")
        # 未过期未消费(应保留)
        await store.callback_nonce_create(
            nonce="keep_003", principal_id=1, action="test", expires_at=future
        )
        now = _dt.datetime.now().isoformat()
        # consumed_before 设为未来 1 秒,确保已消费的都被清理
        future_cutoff = (_dt.datetime.now() + _dt.timedelta(seconds=1)).isoformat()
        result = await store.callback_nonce_cleanup(
            expired_before=now, consumed_before=future_cutoff
        )
        assert result["deleted_expired"] == 1
        assert result["deleted_consumed"] == 1
        assert await store.callback_nonce_exists("expired_003") is False
        assert await store.callback_nonce_exists("consumed_003") is False
        assert await store.callback_nonce_exists("keep_003") is True

    @pytest.mark.asyncio
    async def test_cleanup_does_not_affect_unconsumed_expired(self, store):
        """consumed_before 清理不影响过期但未消费的 nonce(仅 expired_before 清理)。"""
        past = (_dt.datetime.now() - _dt.timedelta(hours=1)).isoformat()
        await store.callback_nonce_create(
            nonce="expired_unconsumed", principal_id=1, action="test", expires_at=past
        )
        # 仅传 consumed_before,不传 expired_before
        future_cutoff = (_dt.datetime.now() + _dt.timedelta(hours=1)).isoformat()
        result = await store.callback_nonce_cleanup(consumed_before=future_cutoff)
        assert result["deleted_expired"] == 0
        assert result["deleted_consumed"] == 0
        # 过期但未消费的 nonce 仍存在(未被 expired_before 清理)
        assert await store.callback_nonce_exists("expired_unconsumed") is True
