"""R63 P1-11: 错误码启动校验对 locale 文件 fail-closed 测试。

审计报告要求 (P1-11):
    ErrorDefinition 的三个交互字段已显式化,是实质进步;但 locale 文件读取异常时
    启动校验只 warning。Release 镜像应在启动前对打包后的实际 locale 文件
    fail-closed,而不能只依赖源码 CI。

测试覆盖:
    1. 缺失 locale 文件 → validate_locales() 返回错误 (fail-closed)
    2. locale 文件 JSON 解析失败 → validate_locales() 返回错误
    3. message_key 缺失 → validate_locales() 返回错误
    4. 占位符不对称 → validate_locales() 返回错误
    5. ERROR_CODES_LOCALE_STRICT=0 降级为 warning (不抛异常)
    6. 所有 ErrorDefinition 都有有效的 locale 条目 (集成测试)
    7. zh-CN 和 en-US key 对称
    8. 无逃生门时损坏的 locale 文件导致启动错误 (fail-closed 默认行为)
    9. LOCALE_VALIDATION_FAILED 错误码已注册且有 locale 条目

测试策略:
    - 测试 1-4: 使用 validate_locales(locales_dir=tmp_path) 隔离测试不同 locale 文件
    - 测试 5, 8: 使用 subprocess 重新导入模块,测试启动期 fail-closed / 逃生门行为
    - 测试 6-7, 9: 使用真实 locale 文件的集成测试
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试环境兼容(mock telegram 库)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "locales"


# ════════════════════════════════════════════════════════════════
# Fixture: ErrorRegistry 重置(用例间隔离)
# ════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def reset_error_registry():
    """每个用例前重置 ErrorRegistry(避免用例间污染)。"""
    from services.error_codes import ErrorRegistry
    ErrorRegistry.reset()
    yield
    ErrorRegistry.reset()


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════
def _write_locale_files(tmp_path: Path, zh_content: dict, en_content: dict) -> None:
    """写入临时 locale 文件(zh-CN.json + en-US.json)。"""
    (tmp_path / "zh-CN.json").write_text(
        json.dumps(zh_content, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "en-US.json").write_text(
        json.dumps(en_content, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ════════════════════════════════════════════════════════════════
# 1-4: validate_locales() 单元测试(使用临时目录隔离)
# ════════════════════════════════════════════════════════════════
class TestValidateLocalesUnit:
    """validate_locales() 方法的单元测试。"""

    def test_missing_locale_file_returns_errors(self, tmp_path):
        """测试 1: 缺失 locale 文件 → validate_locales() 返回错误。"""
        from services.error_codes import ErrorRegistry

        # tmp_path 是空目录,没有 locale 文件
        errors = ErrorRegistry.validate_locales(locales_dir=tmp_path)
        assert len(errors) >= 2, (
            f"Expected at least 2 errors (zh-CN + en-US missing), got {len(errors)}"
        )
        assert any("zh-CN" in e and "missing" in e.lower() for e in errors), (
            f"Expected zh-CN missing error, got: {errors}"
        )
        assert any("en-US" in e and "missing" in e.lower() for e in errors), (
            f"Expected en-US missing error, got: {errors}"
        )

    def test_invalid_json_returns_errors(self, tmp_path):
        """测试 2: locale 文件 JSON 解析失败 → validate_locales() 返回错误。"""
        from services.error_codes import ErrorRegistry

        # 写入无效 JSON
        (tmp_path / "zh-CN.json").write_text("{invalid json !!!", encoding="utf-8")
        (tmp_path / "en-US.json").write_text("{also invalid !!!", encoding="utf-8")

        errors = ErrorRegistry.validate_locales(locales_dir=tmp_path)
        assert len(errors) >= 2, (
            f"Expected at least 2 JSON parse errors, got {len(errors)}"
        )
        assert any("zh-CN" in e and ("parse" in e.lower() or "json" in e.lower()) for e in errors), (
            f"Expected zh-CN JSON parse error, got: {errors}"
        )
        assert any("en-US" in e and ("parse" in e.lower() or "json" in e.lower()) for e in errors), (
            f"Expected en-US JSON parse error, got: {errors}"
        )

    def test_missing_message_key_returns_errors(self, tmp_path):
        """测试 3: message_key 缺失 → validate_locales() 返回错误。"""
        from services.error_codes import ErrorDefinition, ErrorRegistry

        # 注册一个测试 definition,message_key 不在 locale 文件中
        ErrorRegistry.register(ErrorDefinition(
            code="TEST.LOCALE.MISSING_KEY",
            message_key="errors.test.missing_key",
            http_status=500,
            retryable=False,
            severity="error",
            safe_params=[],
            presentation="short_hint",
            show_retry_button=False,
            audit_level="warning",
        ))

        # 创建 locale 文件(不含 errors.test.missing_key,但两文件 key 对称)
        _write_locale_files(
            tmp_path,
            {"errors": {"other": "some value"}},
            {"errors": {"other": "some value"}},
        )

        errors = ErrorRegistry.validate_locales(locales_dir=tmp_path)
        assert len(errors) > 0, "Expected errors for missing message_key"
        # 应报告 zh-CN 和 en-US 都缺失
        missing_key_errors = [
            e for e in errors if "missing_key" in e and "missing in" in e
        ]
        assert len(missing_key_errors) >= 2, (
            f"Expected at least 2 missing message_key errors "
            f"(zh-CN + en-US), got {len(missing_key_errors)}: {errors}"
        )

    def test_placeholder_mismatch_returns_errors(self, tmp_path):
        """测试 4: 占位符不对称 → validate_locales() 返回错误。"""
        from services.error_codes import ErrorDefinition, ErrorRegistry

        # 注册一个测试 definition,message_key 在 locale 文件中存在
        ErrorRegistry.register(ErrorDefinition(
            code="TEST.LOCALE.PLACEHOLDER_MISMATCH",
            message_key="errors.test.mismatch",
            http_status=500,
            retryable=False,
            severity="error",
            safe_params=[],
            presentation="short_hint",
            show_retry_button=False,
            audit_level="warning",
        ))

        # 创建 locale 文件:zh-CN 有 {user_id},en-US 有 {other_id}
        # 两个文件 key 对称(都只有 errors.test.mismatch),但占位符不同
        _write_locale_files(
            tmp_path,
            {"errors": {"test": {"mismatch": "value {user_id}"}}},
            {"errors": {"test": {"mismatch": "value {other_id}"}}},
        )

        errors = ErrorRegistry.validate_locales(locales_dir=tmp_path)
        assert len(errors) > 0, "Expected errors for placeholder mismatch"
        placeholder_errors = [
            e for e in errors
            if "placeholder" in e.lower() or "mismatch" in e.lower()
        ]
        assert len(placeholder_errors) > 0, (
            f"Expected placeholder mismatch error, got: {errors}"
        )

    def test_valid_locales_return_no_errors(self, tmp_path):
        """测试: 有效的 locale 文件 → validate_locales() 返回空列表。"""
        from services.error_codes import ErrorDefinition, ErrorRegistry

        # 注册一个测试 definition,message_key 在 locale 文件中存在
        ErrorRegistry.register(ErrorDefinition(
            code="TEST.LOCALE.VALID",
            message_key="errors.test.valid",
            http_status=500,
            retryable=False,
            severity="error",
            safe_params=[],
            presentation="short_hint",
            show_retry_button=False,
            audit_level="warning",
        ))

        # 创建有效的 locale 文件(key 对称,占位符一致)
        _write_locale_files(
            tmp_path,
            {"errors": {"test": {"valid": "value {user_id}"}}},
            {"errors": {"test": {"valid": "value {user_id}"}}},
        )

        errors = ErrorRegistry.validate_locales(locales_dir=tmp_path)
        assert errors == [], (
            f"Expected no errors for valid locale files, got: {errors}"
        )


# ════════════════════════════════════════════════════════════════
# 5, 8: 启动期 fail-closed / 逃生门行为(subprocess 测试)
# ════════════════════════════════════════════════════════════════
class TestStartupFailClosedBehavior:
    """测试启动期 fail-closed 行为及 ERROR_CODES_LOCALE_STRICT 逃生门。

    使用 subprocess 重新导入模块,以测试模块加载块的启动期行为。
    """

    @staticmethod
    def _backup_locale_files() -> tuple[str, str]:
        """备份真实 locale 文件,返回 (zh_backup, en_backup) 内容。"""
        zh_path = LOCALES_DIR / "zh-CN.json"
        en_path = LOCALES_DIR / "en-US.json"
        return (
            zh_path.read_text(encoding="utf-8"),
            en_path.read_text(encoding="utf-8"),
        )

    @staticmethod
    def _restore_locale_files(zh_backup: str, en_backup: str) -> None:
        """恢复真实 locale 文件。"""
        zh_path = LOCALES_DIR / "zh-CN.json"
        en_path = LOCALES_DIR / "en-US.json"
        zh_path.write_text(zh_backup, encoding="utf-8")
        en_path.write_text(en_backup, encoding="utf-8")

    @staticmethod
    def _write_broken_locale_files() -> None:
        """写入损坏的 locale 文件(无效 JSON)。"""
        zh_path = LOCALES_DIR / "zh-CN.json"
        en_path = LOCALES_DIR / "en-US.json"
        zh_path.write_text("{invalid json broken !!!", encoding="utf-8")
        en_path.write_text("{invalid json broken !!!", encoding="utf-8")

    def test_escape_hatch_downgrades_to_warning(self):
        """测试 5: ERROR_CODES_LOCALE_STRICT=0 降级为 warning (不抛异常)。

        损坏 locale 文件后,设置 ERROR_CODES_LOCALE_STRICT=0,
        子进程导入模块应成功(仅 warning,不 raise)。
        """
        zh_backup, en_backup = self._backup_locale_files()
        try:
            self._write_broken_locale_files()

            env = dict(os.environ)
            env["ERROR_CODES_LOCALE_STRICT"] = "0"

            result = subprocess.run(
                [sys.executable, "-c",
                 "import services.error_codes; print('IMPORT_OK')"],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO_ROOT),
                timeout=30,
            )

            assert result.returncode == 0, (
                f"Expected import to succeed with ERROR_CODES_LOCALE_STRICT=0, "
                f"but exit code={result.returncode}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
            assert "IMPORT_OK" in result.stdout, (
                f"Expected IMPORT_OK in stdout, got: {result.stdout}"
            )
        finally:
            self._restore_locale_files(zh_backup, en_backup)

    def test_fail_closed_without_escape_hatch(self):
        """测试 8: 无逃生门时损坏的 locale 文件导致启动错误 (fail-closed)。

        损坏 locale 文件后,不设置 ERROR_CODES_LOCALE_STRICT(默认 fail-closed),
        子进程导入模块应失败(exit code != 0)。
        """
        zh_backup, en_backup = self._backup_locale_files()
        try:
            self._write_broken_locale_files()

            env = dict(os.environ)
            # 确保未设置逃生门(默认 fail-closed)
            env.pop("ERROR_CODES_LOCALE_STRICT", None)

            result = subprocess.run(
                [sys.executable, "-c", "import services.error_codes"],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO_ROOT),
                timeout=30,
            )

            assert result.returncode != 0, (
                f"Expected import to FAIL without escape hatch (fail-closed), "
                f"but exit code=0\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
            # stderr 应包含 LOCALE_VALIDATION_FAILED 或 locale validation 错误
            combined = result.stderr + result.stdout
            assert (
                "LOCALE" in combined
                or "locale" in combined.lower()
                or "validation" in combined.lower()
            ), (
                f"Expected locale validation error in output, "
                f"got stderr: {result.stderr}\nstdout: {result.stdout}"
            )
        finally:
            self._restore_locale_files(zh_backup, en_backup)

    def test_locale_files_restored_after_subprocess_tests(self):
        """测试: subprocess 测试后 locale 文件已恢复(防污染)。"""
        # 此测试在 fail-closed / escape_hatch 测试之后运行,验证 locale 文件完好
        zh_data = json.loads(
            (LOCALES_DIR / "zh-CN.json").read_text(encoding="utf-8")
        )
        en_data = json.loads(
            (LOCALES_DIR / "en-US.json").read_text(encoding="utf-8")
        )
        assert isinstance(zh_data, dict), "zh-CN.json should be valid JSON dict"
        assert isinstance(en_data, dict), "en-US.json should be valid JSON dict"


# ════════════════════════════════════════════════════════════════
# 6-7, 9: 集成测试(使用真实 locale 文件)
# ════════════════════════════════════════════════════════════════
class TestLocaleIntegration:
    """使用真实 locale 文件的集成测试。"""

    def test_all_error_definitions_have_locale_entries(self):
        """测试 6: 所有 ErrorDefinition 都有有效的 locale 条目 (集成测试)。

        当前 locale 文件必须通过 validate_locales() 校验(0 errors)。
        """
        from services.error_codes import ErrorRegistry

        # 触发默认定义注册(reset 后 _definitions 为空,需重新初始化)
        ErrorRegistry._ensure_initialized()

        errors = ErrorRegistry.validate_locales()
        assert errors == [], (
            f"Current locale files should pass validation, but got "
            f"{len(errors)} error(s):\n" + "\n".join(f"  - {e}" for e in errors)
        )

    def test_zh_cn_en_us_symmetric_keys(self):
        """测试 7: zh-CN 和 en-US key 对称(无缺失/多余)。"""
        zh_data = json.loads(
            (LOCALES_DIR / "zh-CN.json").read_text(encoding="utf-8")
        )
        en_data = json.loads(
            (LOCALES_DIR / "en-US.json").read_text(encoding="utf-8")
        )

        def flatten(d: dict, prefix: str = "") -> set:
            keys: set = set()
            for k, v in d.items():
                full = f"{prefix}.{k}" if prefix else str(k)
                if isinstance(v, dict):
                    keys.update(flatten(v, full))
                else:
                    keys.add(full)
            return keys

        zh_keys = flatten(zh_data)
        en_keys = flatten(en_data)

        only_zh = zh_keys - en_keys
        only_en = en_keys - zh_keys

        assert not only_zh, (
            f"Keys in zh-CN but NOT in en-US ({len(only_zh)} keys): "
            f"{sorted(only_zh)[:10]}"
        )
        assert not only_en, (
            f"Keys in en-US but NOT in zh-CN ({len(only_en)} keys): "
            f"{sorted(only_en)[:10]}"
        )

    def test_locale_validation_failed_error_code_registered(self):
        """测试 9: LOCALE_VALIDATION_FAILED 错误码已注册且有 locale 条目。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        # 触发默认定义注册
        ErrorRegistry._ensure_initialized()

        # 1. 错误码常量存在
        assert hasattr(ErrorCodes, "LOCALE_VALIDATION_FAILED"), (
            "ErrorCodes.LOCALE_VALIDATION_FAILED should exist"
        )
        assert ErrorCodes.LOCALE_VALIDATION_FAILED == "LOCALE.VALIDATION.FAILED"

        # 2. ErrorDefinition 已注册
        assert ErrorRegistry.is_registered(ErrorCodes.LOCALE_VALIDATION_FAILED), (
            "LOCALE_VALIDATION_FAILED should be registered in ErrorRegistry"
        )

        # 3. message_key 在两个 locale 文件中均存在
        defn = ErrorRegistry.get(ErrorCodes.LOCALE_VALIDATION_FAILED)
        message_key = defn.message_key

        zh_data = json.loads(
            (LOCALES_DIR / "zh-CN.json").read_text(encoding="utf-8")
        )
        en_data = json.loads(
            (LOCALES_DIR / "en-US.json").read_text(encoding="utf-8")
        )

        def flatten(d: dict, prefix: str = "") -> set:
            keys: set = set()
            for k, v in d.items():
                full = f"{prefix}.{k}" if prefix else str(k)
                if isinstance(v, dict):
                    keys.update(flatten(v, full))
                else:
                    keys.add(full)
            return keys

        zh_keys = flatten(zh_data)
        en_keys = flatten(en_data)

        assert message_key in zh_keys, (
            f"message_key '{message_key}' missing in zh-CN.json"
        )
        assert message_key in en_keys, (
            f"message_key '{message_key}' missing in en-US.json"
        )

    def test_validate_locales_default_path_passes(self):
        """测试: 使用默认路径(真实 locale 文件)调用 validate_locales() 通过。"""
        from services.error_codes import ErrorRegistry

        ErrorRegistry._ensure_initialized()

        # 使用默认路径(不传 locales_dir)
        errors = ErrorRegistry.validate_locales()
        assert errors == [], (
            f"validate_locales() with default path should return no errors, "
            f"but got: {errors}"
        )
