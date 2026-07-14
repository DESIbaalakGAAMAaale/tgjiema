"""R49 P1-b 终审整改测试:scanner 实际工作 + production token 检查。

R48 scanner 输出"未扫描到任何调用点"是错误的——代码库中有大量
CallbackQueryHandler 注册点,但旧 scanner 仅扫描 4 个 API 函数的直接调用。

R49 整改:
1. scanner 新增 CallbackQueryHandler 注册点检测
2. scanner 新增模式匹配高风险 action(包含 delete/ban/purge 等子串)
3. 每个Bot启动函数必须调用 validate_production_config(fail-closed)

测试范围:
- test_scanner_finds_callback_handlers: scanner 应找到 ≥1 个 CallbackQueryHandler
- test_scanner_detects_high_risk_action_using_sync_api: 旧 sync API + 高风险 action → 违规
- test_scanner_passes_when_using_async_nonce_api: 新 async API + 高风险 action → 通过
- test_validate_production_config_called_in_bot_startup: 5 个 Bot 启动文件 AST 检查
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# mock telegram(避免 ImportError,conftest 已处理,此处兜底)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


# ── 辅助函数 ──────────────────────────────────────────


def _load_scanner_module():
    """通过 importlib 加载 check_button_nonce_coverage.py 为独立模块实例。

    每次返回全新的模块对象,避免跨用例状态污染。
    """
    spec = importlib.util.spec_from_file_location(
        "_check_button_nonce_coverage_r49_test",
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

    Returns:
        (exit_code, violations, info)
    """
    fake_root = tmp_path / "fake_repo_r49"
    fake_root.mkdir()
    for rel_path, content in files.items():
        full_path = fake_root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")

    mod = _load_scanner_module()
    mod.REPO_ROOT = fake_root
    return mod.check()


@pytest.fixture(autouse=True)
def setup_bot_token(monkeypatch):
    """为 button_security 提供固定 BOT_TOKEN(避免 MagicMock 导致 HMAC 失败)。"""
    import config
    monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "r49_test_admin_bot_token")
    monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "r49_test_sender_bot_token")
    monkeypatch.setattr(config.settings, "ENVIRONMENT", "development")


# ════════════════════════════════════════════════════════════════
# 1. scanner 应找到 CallbackQueryHandler 注册点
# ════════════════════════════════════════════════════════════════


class TestScannerFindsCallbackHandlers:
    """R49 P1-b: scanner 应该找到至少 1 个 CallbackQueryHandler 注册点。

    R48 scanner 仅扫描 4 个 API 函数直接调用,遗漏了 CallbackQueryHandler 注册点,
    导致输出"未扫描到任何调用点"。R49 修复后应能找到 bots/ 下的注册点。
    """

    def test_scanner_finds_callback_handlers(self):
        """scanner 在真实代码库中应找到至少 1 个 CallbackQueryHandler 注册点。

        真实代码库中 bots/admin_bot/run.py, bots/dsp_bot.py, bots/idx_bot.py,
        bots/up_bot.py 都有 CallbackQueryHandler 注册。
        """
        mod = _load_scanner_module()
        exit_code, violations, info = mod.check()

        # 从 info 中筛选 CallbackQueryHandler 注册点
        callback_handlers = [
            i for i in info if i.get("type") == "callback_handler"
        ]
        assert len(callback_handlers) >= 1, (
            f"scanner 应找到至少 1 个 CallbackQueryHandler 注册点, "
            f"实际找到 {len(callback_handlers)} 个。"
            f"info 总数={len(info)}"
        )

    def test_scanner_finds_callback_handlers_in_bots_dir(self):
        """scanner 应在 bots/ 目录下找到 CallbackQueryHandler 注册点。"""
        mod = _load_scanner_module()
        exit_code, violations, info = mod.check()

        callback_handlers = [
            i for i in info
            if i.get("type") == "callback_handler" and "bots/" in i.get("file", "")
        ]
        assert len(callback_handlers) >= 1, (
            f"scanner 应在 bots/ 目录下找到 CallbackQueryHandler 注册点, "
            f"实际找到 {len(callback_handlers)} 个"
        )

    def test_scanner_finds_admin_bot_callback_handler(self):
        """scanner 应在 bots/admin_bot/run.py 中找到 CallbackQueryHandler。"""
        mod = _load_scanner_module()
        exit_code, violations, info = mod.check()

        admin_handlers = [
            i for i in info
            if i.get("type") == "callback_handler"
            and "admin_bot" in i.get("file", "")
        ]
        assert len(admin_handlers) >= 1, (
            f"scanner 应在 bots/admin_bot/ 中找到 CallbackQueryHandler, "
            f"实际找到 {len(admin_handlers)} 个"
        )

    def test_scanner_finds_dsp_bot_callback_handler(self):
        """scanner 应在 bots/dsp_bot.py 中找到 CallbackQueryHandler。"""
        mod = _load_scanner_module()
        exit_code, violations, info = mod.check()

        dsp_handlers = [
            i for i in info
            if i.get("type") == "callback_handler"
            and "dsp_bot" in i.get("file", "")
        ]
        assert len(dsp_handlers) >= 1, (
            f"scanner 应在 bots/dsp_bot.py 中找到 CallbackQueryHandler, "
            f"实际找到 {len(dsp_handlers)} 个"
        )

    def test_scanner_callback_handler_has_uses_verify_field(self):
        """CallbackQueryHandler info 项应包含 uses_verify 字段。"""
        mod = _load_scanner_module()
        exit_code, violations, info = mod.check()

        callback_handlers = [
            i for i in info if i.get("type") == "callback_handler"
        ]
        assert len(callback_handlers) >= 1
        for handler in callback_handlers:
            assert "uses_verify" in handler, (
                f"CallbackQueryHandler info 项应包含 uses_verify 字段: {handler}"
            )


# ════════════════════════════════════════════════════════════════
# 2. scanner 检测高风险 action 使用旧 sync API
# ════════════════════════════════════════════════════════════════


class TestScannerDetectsHighRiskSyncApi:
    """R49 P1-b: 高风险 action(含 delete/ban/purge 等子串)使用旧 sync API → 违规。

    R49 新增模式匹配:action 包含 delete/ban/purge/takedown 等子串也视为高风险,
    不再仅依赖 HIGH_RISK_ACTIONS 精确匹配。
    """

    def test_scanner_detects_high_risk_action_using_sync_api(self, tmp_path):
        """高风险 action(delete_user)使用 generate_signed_callback → 违规。

        delete_user 不在 HIGH_RISK_ACTIONS 精确集合中,但包含 "delete" 子串,
        R49 模式匹配应识别为高风险。
        """
        files = {
            "bots/bad_delete.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(action=\"delete_user\", user_id=1, data=\"x\")\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 1, (
            f"高风险 action 'delete_user' 使用旧 sync API 应报告违规, "
            f"violations={violations}"
        )
        assert len(violations) == 1
        assert violations[0]["action"] == "delete_user"
        assert violations[0]["func"] == "generate_signed_callback"

    def test_scanner_detects_ban_user_sync_api(self, tmp_path):
        """高风险 action(ban_user)使用 generate_signed_callback → 违规。"""
        files = {
            "bots/bad_ban.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(action=\"ban_user\", user_id=1, data=\"x\")\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 1
        assert len(violations) == 1
        assert violations[0]["action"] == "ban_user"

    def test_scanner_detects_purge_files_sync_api(self, tmp_path):
        """高风险 action(purge_files)使用 generate_signed_callback → 违规。"""
        files = {
            "bots/bad_purge.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(action=\"purge_files\", user_id=1, data=\"x\")\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 1
        assert len(violations) == 1
        assert violations[0]["action"] == "purge_files"

    def test_scanner_detects_takedown_content_sync_api(self, tmp_path):
        """高风险 action(takedown_content)使用 generate_signed_callback → 违规。"""
        files = {
            "bots/bad_takedown.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(action=\"takedown_content\", user_id=1, data=\"x\")\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 1
        assert len(violations) == 1
        assert violations[0]["action"] == "takedown_content"

    def test_scanner_violation_output_format(self, tmp_path):
        """违规输出应包含 file, line, action, violation_type 字段。"""
        files = {
            "bots/bad_format.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(action=\"delete_user\", user_id=1, data=\"x\")\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 1
        v = violations[0]
        assert "file" in v
        assert "line" in v
        assert "action" in v
        assert "violation_type" in v
        assert "reason" in v
        assert v["violation_type"] == "HIGH_RISK_SYNC_API"


# ════════════════════════════════════════════════════════════════
# 3. scanner 通过:高风险 action 使用新 async API
# ════════════════════════════════════════════════════════════════


class TestScannerPassesAsyncNonceApi:
    """R49 P1-b: 高风险 action 使用新 async nonce API → 通过(无违规)。"""

    def test_scanner_passes_when_using_async_nonce_api(self, tmp_path):
        """高风险 action(delete_user)使用 sign_button_token_with_nonce → 通过。

        sign_button_token_with_nonce 是 R47 async API(持久化 nonce),
        高风险 action 应使用此 API 而非旧 sync generate_signed_callback。
        """
        files = {
            "bots/good_async.py": (
                "from services.button_security import sign_button_token_with_nonce\n"
                "await sign_button_token_with_nonce(action=\"delete_user\", principal_id=1, payload=\"x\")\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0, (
            f"高风险 action 使用 async nonce API 应通过, violations={violations}"
        )
        assert violations == []

    def test_scanner_passes_ban_user_async(self, tmp_path):
        """高风险 action(ban_user)使用 sign_button_token_with_nonce → 通过。"""
        files = {
            "bots/good_ban.py": (
                "from services.button_security import sign_button_token_with_nonce\n"
                "await sign_button_token_with_nonce(action=\"ban_user\", principal_id=1, payload=\"x\")\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_scanner_passes_purge_files_async(self, tmp_path):
        """高风险 action(purge_files)使用 sign_button_token_with_nonce → 通过。"""
        files = {
            "bots/good_purge.py": (
                "from services.button_security import sign_button_token_with_nonce\n"
                "await sign_button_token_with_nonce(action=\"purge_files\", principal_id=1, payload=\"x\")\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []

    def test_scanner_low_risk_action_sync_api_still_allowed(self, tmp_path):
        """低风险 action(cancel)使用旧 sync API → 仍允许(向后兼容)。"""
        files = {
            "bots/low_risk.py": (
                "from services.button_security import generate_signed_callback\n"
                "generate_signed_callback(action=\"cancel\", user_id=1, data=\"x\")\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        assert exit_code == 0
        assert violations == []


# ════════════════════════════════════════════════════════════════
# 4. validate_production_config 在 Bot 启动函数中调用
# ════════════════════════════════════════════════════════════════


class TestValidateProductionConfigCalledInBotStartup:
    """R49 P1-b: 每个 Bot 的启动函数应调用 validate_production_config()。

    R48 终审整改:production secret 检查必须在每个 Bot 启动时实际触发,
    而不是仅依赖模块被导入时偶然执行。

    检查 5 个 Bot 启动文件:
      - bots/admin_bot/run.py
      - bots/dsp_bot.py
      - bots/up_bot.py
      - bots/idx_bot.py
      - bots/mon_bot.py
    """

    # Bot 启动文件相对路径
    BOT_STARTUP_FILES: list[str] = [
        "bots/admin_bot/run.py",
        "bots/dsp_bot.py",
        "bots/up_bot.py",
        "bots/idx_bot.py",
        "bots/mon_bot.py",
    ]

    def _check_file_calls_validate_production_config(self, filepath: Path) -> bool:
        """用 AST 检查文件中是否调用了 validate_production_config()。

        检测模式:
          1. validate_production_config()  — ast.Name 直接调用
          2. button_security.validate_production_config()  — ast.Attribute
          3. bs.validate_production_config()  — ast.Attribute (别名)

        Returns:
            True 如果文件中至少有一处调用 validate_production_config
        """
        try:
            source = filepath.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(filepath))
        except (SyntaxError, UnicodeDecodeError, OSError):
            return False

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "validate_production_config":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "validate_production_config":
                return True
        return False

    def test_validate_production_config_called_in_bot_startup(self):
        """5 个 Bot 启动文件都应调用 validate_production_config()。"""
        missing = []
        for rel_path in self.BOT_STARTUP_FILES:
            filepath = REPO_ROOT / rel_path
            assert filepath.exists(), f"Bot 启动文件不存在: {rel_path}"
            if not self._check_file_calls_validate_production_config(filepath):
                missing.append(rel_path)

        assert missing == [], (
            f"以下 Bot 启动文件未调用 validate_production_config(): {missing}。"
            f"每个 Bot 启动函数应在 early stage 调用 validate_production_config()"
            f"以确保 production 环境 fail-closed(缺 BOT_TOKEN 时启动失败)。"
        )

    def test_admin_bot_run_calls_validate_production_config(self):
        """bots/admin_bot/run.py 应调用 validate_production_config()。"""
        filepath = REPO_ROOT / "bots/admin_bot/run.py"
        assert self._check_file_calls_validate_production_config(filepath), (
            "bots/admin_bot/run.py 未调用 validate_production_config()"
        )

    def test_dsp_bot_calls_validate_production_config(self):
        """bots/dsp_bot.py 应调用 validate_production_config()。"""
        filepath = REPO_ROOT / "bots/dsp_bot.py"
        assert self._check_file_calls_validate_production_config(filepath), (
            "bots/dsp_bot.py 未调用 validate_production_config()"
        )

    def test_up_bot_calls_validate_production_config(self):
        """bots/up_bot.py 应调用 validate_production_config()。"""
        filepath = REPO_ROOT / "bots/up_bot.py"
        assert self._check_file_calls_validate_production_config(filepath), (
            "bots/up_bot.py 未调用 validate_production_config()"
        )

    def test_idx_bot_calls_validate_production_config(self):
        """bots/idx_bot.py 应调用 validate_production_config()。"""
        filepath = REPO_ROOT / "bots/idx_bot.py"
        assert self._check_file_calls_validate_production_config(filepath), (
            "bots/idx_bot.py 未调用 validate_production_config()"
        )

    def test_mon_bot_calls_validate_production_config(self):
        """bots/mon_bot.py 应调用 validate_production_config()。"""
        filepath = REPO_ROOT / "bots/mon_bot.py"
        assert self._check_file_calls_validate_production_config(filepath), (
            "bots/mon_bot.py 未调用 validate_production_config()"
        )


# ════════════════════════════════════════════════════════════════
# 5. CallbackQueryHandler 检测单元测试(临时文件)
# ════════════════════════════════════════════════════════════════


class TestScannerCallbackHandlerDetection:
    """R49 P1-b: scanner 应正确检测 CallbackQueryHandler 注册点。"""

    def test_scanner_detects_callback_handler_in_temp_file(self, tmp_path):
        """scanner 应检测临时文件中的 CallbackQueryHandler 注册。"""
        files = {
            "bots/test_bot.py": (
                "from telegram.ext import CallbackQueryHandler\n"
                "async def my_callback(update, context):\n"
                "    pass\n"
                "app.add_handler(CallbackQueryHandler(my_callback, pattern=r'^test\\|'))\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        callback_handlers = [
            i for i in info if i.get("type") == "callback_handler"
        ]
        assert len(callback_handlers) == 1
        assert callback_handlers[0]["callback"] == "my_callback"
        assert "test" in callback_handlers[0]["pattern"]

    def test_scanner_detects_high_risk_pattern(self, tmp_path):
        """scanner 应识别包含 restore 的高风险 pattern。"""
        files = {
            "bots/admin_bot.py": (
                "from telegram.ext import CallbackQueryHandler\n"
                "async def menu_callback(update, context):\n"
                "    pass\n"
                "app.add_handler(CallbackQueryHandler(menu_callback, "
                "pattern=r'^(menu:|action:|restore:)'))\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        callback_handlers = [
            i for i in info if i.get("type") == "callback_handler"
        ]
        assert len(callback_handlers) == 1
        assert callback_handlers[0]["is_high_risk"] is True, (
            "pattern 包含 'restore' 应识别为高风险"
        )

    def test_scanner_detects_low_risk_pattern(self, tmp_path):
        """scanner 应识别低风险 pattern(不含高风险子串)。"""
        files = {
            "bots/low_risk_bot.py": (
                "from telegram.ext import CallbackQueryHandler\n"
                "async def pagination_callback(update, context):\n"
                "    pass\n"
                "app.add_handler(CallbackQueryHandler(pagination_callback, "
                "pattern=r'^(pg\\||noop$)'))\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        callback_handlers = [
            i for i in info if i.get("type") == "callback_handler"
        ]
        assert len(callback_handlers) == 1
        assert callback_handlers[0]["is_high_risk"] is False

    def test_scanner_detects_callback_using_verify_button_token(self, tmp_path):
        """scanner 应检测 callback 函数中调用 verify_button_token(async)。"""
        files = {
            "bots/secure_bot.py": (
                "from telegram.ext import CallbackQueryHandler\n"
                "from services.button_security import verify_button_token\n"
                "async def secure_callback(update, context):\n"
                "    query = update.callback_query\n"
                "    valid, action, data = await verify_button_token(query.data, 1)\n"
                "    if not valid:\n"
                "        return\n"
                "app.add_handler(CallbackQueryHandler(secure_callback, pattern=r'^secure\\|'))\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        callback_handlers = [
            i for i in info if i.get("type") == "callback_handler"
        ]
        assert len(callback_handlers) == 1
        assert callback_handlers[0]["uses_verify"] == "async"

    def test_scanner_detects_callback_using_verify_signed_callback(self, tmp_path):
        """scanner 应检测 callback 函数中调用 verify_signed_callback(sync)。"""
        files = {
            "bots/legacy_bot.py": (
                "from telegram.ext import CallbackQueryHandler\n"
                "from services.button_security import verify_signed_callback\n"
                "async def legacy_callback(update, context):\n"
                "    query = update.callback_query\n"
                "    valid, action, data = verify_signed_callback(query.data, 1)\n"
                "    if not valid:\n"
                "        return\n"
                "app.add_handler(CallbackQueryHandler(legacy_callback, pattern=r'^legacy\\|'))\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        callback_handlers = [
            i for i in info if i.get("type") == "callback_handler"
        ]
        assert len(callback_handlers) == 1
        assert callback_handlers[0]["uses_verify"] == "sync"

    def test_scanner_detects_callback_without_verify(self, tmp_path):
        """scanner 应检测 callback 函数未调用任何 verify API(uses_verify=None)。"""
        files = {
            "bots/no_verify_bot.py": (
                "from telegram.ext import CallbackQueryHandler\n"
                "async def no_verify_callback(update, context):\n"
                "    query = update.callback_query\n"
                "    await query.answer()\n"
                "app.add_handler(CallbackQueryHandler(no_verify_callback, pattern=r'^nov\\|'))\n"
            ),
        }
        exit_code, violations, info = _run_check(tmp_path, files)
        callback_handlers = [
            i for i in info if i.get("type") == "callback_handler"
        ]
        assert len(callback_handlers) == 1
        assert callback_handlers[0]["uses_verify"] is None
