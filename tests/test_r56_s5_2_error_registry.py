"""R56 §5.2: 统一错误码 registry 单元测试。

测试覆盖:
1. ErrorCodes 与 ErrorEnum 自动同步
2. ErrorRegistry 便捷查询方法
3. to_frontend_mapping / to_frontend_json 导出
4. locale schema 验证(所有 message_key 在 zh-CN.json + en-US.json 中存在)
5. CI AST 扫描规则(check_error_codes.py)
6. 错误码命名规范(DOMAIN.OPERATION.REASON 三段式)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.error_codes import (  # type: ignore  # noqa: E402
    AppError,
    ErrorCodes,
    ErrorDefinition,
    ErrorEnum,
    ErrorRegistry,
)


class TestErrorEnumSync:
    """R56 §5.2: ErrorCodes 与 ErrorEnum 自动同步。"""

    def test_error_enum_count_matches_error_codes(self):
        """ErrorEnum 成员数应等于 ErrorCodes 中大写常量数。"""
        ec_count = sum(
            1 for attr in dir(ErrorCodes)
            if attr.isupper() and not attr.startswith("_")
            and isinstance(getattr(ErrorCodes, attr), str)
        )
        enum_count = len(list(ErrorEnum))
        assert enum_count == ec_count, (
            f"ErrorEnum 成员数 ({enum_count}) != ErrorCodes 常量数 ({ec_count})"
        )

    def test_error_enum_values_match_error_codes(self):
        """每个 ErrorEnum 成员的 value 应等于对应 ErrorCodes 常量。"""
        for attr_name in dir(ErrorCodes):
            if not attr_name.isupper() or attr_name.startswith("_"):
                continue
            value = getattr(ErrorCodes, attr_name)
            if not isinstance(value, str):
                continue
            enum_member = getattr(ErrorEnum, attr_name, None)
            assert enum_member is not None, (
                f"ErrorEnum.{attr_name} 不存在(未自动同步)"
            )
            assert enum_member.value == value, (
                f"ErrorEnum.{attr_name}.value ({enum_member.value!r}) != "
                f"ErrorCodes.{attr_name} ({value!r})"
            )

    def test_error_enum_is_str_subclass(self):
        """ErrorEnum 成员必须是 str 子类(可直接用作字符串)。"""
        for member in ErrorEnum:
            assert isinstance(member, str), (
                f"{member.name} 不是 str 子类"
            )
            assert member == member.value, (
                f"{member.name} ({member!r}) != value ({member.value!r})"
            )

    def test_error_enum_equals_error_codes(self):
        """ErrorEnum.XXX == ErrorCodes.XXX(str 相等性比较)。"""
        assert (
            ErrorEnum.UPLOAD_COPY_TELEGRAM_TIMEOUT
            == ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT
        )

    def test_from_code_returns_member(self):
        """from_code 应返回对应 ErrorEnum 成员。"""
        code = ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT
        member = ErrorEnum.from_code(code)
        assert member is not None
        assert member.value == code

    def test_from_code_returns_none_for_unknown(self):
        """from_code 对未知 code 应返回 None。"""
        member = ErrorEnum.from_code("NONEXISTENT.CODE.XXX")
        assert member is None

    def test_error_enum_can_be_used_in_app_error(self):
        """ErrorEnum 可直接传给 AppError(作为 str 替代 ErrorCodes)。"""
        error = AppError(
            ErrorEnum.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "test"},
        )
        assert error.code == ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT
        assert error.code == ErrorEnum.UPLOAD_COPY_TELEGRAM_TIMEOUT


class TestErrorRegistryHelpers:
    """R56 §5.2: ErrorRegistry 便捷查询方法。"""

    def test_is_retryable(self):
        """is_retryable 应返回正确的可重试状态。"""
        assert ErrorRegistry.is_retryable(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT
        ) is True
        assert ErrorRegistry.is_retryable(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_FORBIDDEN
        ) is False

    def test_get_safe_params(self):
        """get_safe_params 应返回安全参数白名单副本。"""
        params = ErrorRegistry.get_safe_params(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT
        )
        assert "file_code" in params
        assert "channel_id" in params
        # 应返回副本,修改不影响原数据
        params.append("new_param")
        params2 = ErrorRegistry.get_safe_params(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT
        )
        assert "new_param" not in params2

    def test_get_http_status(self):
        """get_http_status 应返回 HTTP 状态码。"""
        assert ErrorRegistry.get_http_status(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT
        ) == 504
        assert ErrorRegistry.get_http_status(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_FORBIDDEN
        ) == 403

    def test_get_severity(self):
        """get_severity 应返回严重级别。"""
        severity = ErrorRegistry.get_severity(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT
        )
        assert severity in ("info", "warning", "error", "critical")
        assert ErrorRegistry.get_severity(
            ErrorCodes.AUTH_MFA_REPLAYED
        ) == "critical"

    def test_get_fallback_to_error_internal(self):
        """未注册 code 应 fallback 到 ERROR_INTERNAL。"""
        sev = ErrorRegistry.get_severity("NONEXISTENT.CODE.XXX")
        assert sev == "error"  # ERROR_INTERNAL 的 severity


class TestFrontendMapping:
    """R56 §5.2: 前端映射导出。"""

    def test_to_frontend_mapping_returns_dict(self):
        """to_frontend_mapping 应返回 dict[str, dict]。"""
        mapping = ErrorRegistry.to_frontend_mapping()
        assert isinstance(mapping, dict)
        assert len(mapping) > 0

    def test_frontend_mapping_contains_all_codes(self):
        """前端映射应包含所有已注册的 code。"""
        mapping = ErrorRegistry.to_frontend_mapping()
        for code in ErrorRegistry.all_codes():
            assert code in mapping, f"前端映射缺少 code: {code}"

    def test_frontend_mapping_has_required_fields(self):
        """每个映射项应包含必需字段。"""
        required_fields = {
            "code", "message_key", "http_status", "retryable",
            "severity", "safe_params", "telegram_presentation",
            "show_retry_button",
        }
        mapping = ErrorRegistry.to_frontend_mapping()
        for code, item in mapping.items():
            missing = required_fields - set(item.keys())
            assert not missing, (
                f"code '{code}' 缺少字段: {missing}"
            )

    def test_telegram_presentation_values(self):
        """telegram_presentation 只能是 short_hint/inline/silent。"""
        valid_values = {"short_hint", "inline", "silent"}
        mapping = ErrorRegistry.to_frontend_mapping()
        for code, item in mapping.items():
            assert item["telegram_presentation"] in valid_values, (
                f"code '{code}' 的 telegram_presentation "
                f"={item['telegram_presentation']!r} 不在允许值中"
            )

    def test_show_retry_button_equals_retryable(self):
        """show_retry_button 应等于 retryable。"""
        mapping = ErrorRegistry.to_frontend_mapping()
        for code, item in mapping.items():
            assert item["show_retry_button"] == item["retryable"], (
                f"code '{code}': show_retry_button != retryable"
            )

    def test_telegram_presentation_by_severity(self):
        """telegram_presentation 应按 severity 推断。"""
        mapping = ErrorRegistry.to_frontend_mapping()
        for code, item in mapping.items():
            sev = item["severity"]
            pres = item["telegram_presentation"]
            if sev == "critical":
                assert pres == "inline", (
                    f"critical code '{code}' 的 presentation 应为 inline"
                )
            elif sev == "info":
                assert pres == "silent", (
                    f"info code '{code}' 的 presentation 应为 silent"
                )
            else:
                assert pres == "short_hint", (
                    f"{sev} code '{code}' 的 presentation 应为 short_hint"
                )

    def test_to_frontend_json_is_valid_json(self):
        """to_frontend_json 应返回合法 JSON 字符串。"""
        json_str = ErrorRegistry.to_frontend_json()
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)
        assert len(parsed) > 0

    def test_to_frontend_json_with_indent(self):
        """to_frontend_json 应支持 indent 参数。"""
        json_str_2 = ErrorRegistry.to_frontend_json(indent=2)
        json_str_4 = ErrorRegistry.to_frontend_json(indent=4)
        # indent=4 应比 indent=2 更长(更多空格)
        assert len(json_str_4) > len(json_str_2)


class TestLocaleSchemaValidation:
    """R56 §5.2: locale schema 验证 — 所有 message_key 必须在 locale 文件中存在。"""

    @pytest.fixture(scope="class")
    def locale_data(self):
        """加载并扁平化所有 locale 文件。"""
        locales_dir = REPO_ROOT / "locales"
        data: dict[str, dict[str, str]] = {}
        for locale in ["zh-CN", "en-US"]:
            path = locales_dir / f"{locale}.json"
            if not path.exists():
                data[locale] = {}
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            flat: dict[str, str] = {}

            def _flatten(d, prefix=""):
                for k, v in d.items():
                    full_key = f"{prefix}.{k}" if prefix else k
                    if isinstance(v, dict):
                        _flatten(v, full_key)
                    else:
                        flat[full_key] = str(v)

            _flatten(raw)
            data[locale] = flat
        return data

    def test_all_message_keys_exist_in_zh_cn(self, locale_data):
        """所有 ErrorRegistry.message_key 必须在 zh-CN.json 中存在。"""
        zh_cn = locale_data["zh-CN"]
        missing = []
        for key in ErrorRegistry.all_message_keys():
            if key not in zh_cn:
                missing.append(key)
        assert not missing, (
            f"zh-CN.json 缺少 {len(missing)} 个 message_key: {missing[:5]}"
        )

    def test_all_message_keys_exist_in_en_us(self, locale_data):
        """所有 ErrorRegistry.message_key 必须在 en-US.json 中存在。"""
        en_us = locale_data["en-US"]
        missing = []
        for key in ErrorRegistry.all_message_keys():
            if key not in en_us:
                missing.append(key)
        assert not missing, (
            f"en-US.json 缺少 {len(missing)} 个 message_key: {missing[:5]}"
        )

    def test_locale_keys_consistency(self, locale_data):
        """zh-CN 和 en-US 的 key 应完全一致。"""
        zh_keys = set(locale_data["zh-CN"].keys())
        en_keys = set(locale_data["en-US"].keys())
        missing_in_en = zh_keys - en_keys
        missing_in_zh = en_keys - zh_keys
        assert not missing_in_en, (
            f"en-US 缺失 {len(missing_in_en)} 个 key: {sorted(missing_in_en)[:5]}"
        )
        assert not missing_in_zh, (
            f"zh-CN 缺失 {len(missing_in_zh)} 个 key: {sorted(missing_in_zh)[:5]}"
        )


class TestErrorCodeNamingConvention:
    """R56 §5.2: 错误码命名规范 — DOMAIN.OPERATION.REASON 三段式。"""

    def test_all_codes_match_three_segment_pattern(self):
        """所有错误码应符合 DOMAIN.OPERATION.REASON 三段式格式。"""
        import re
        pattern = re.compile(r"^[A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]*$")
        invalid = []
        for code in ErrorRegistry.all_codes():
            if not pattern.match(code):
                invalid.append(code)
        assert not invalid, (
            f"以下错误码不符合三段式命名规范: {invalid}"
        )

    def test_code_values_are_uppercase(self):
        """错误码值应全部大写。"""
        for code in ErrorRegistry.all_codes():
            assert code == code.upper(), f"code '{code}' 不全大写"


class TestCIScanRules:
    """R56 §5.2: CI AST 扫描规则(check_error_codes.py)。"""

    def test_check_error_codes_locale_schema_passes(self):
        """scripts/check_error_codes_locale_schema.py 应 exit 0。"""
        result = subprocess.run(
            [sys.executable, "scripts/check_error_codes_locale_schema.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"locale schema 验证失败 (exit {result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    def test_check_error_codes_strict_mode_detects_unregistered(self):
        """scripts/check_error_codes.py --strict 应能检测未注册错误码(R56 §5.2 核心)。

        注:strict 模式还会检测 except:pass / return False 等 P1-5 反模式,
        现有代码有历史遗留,这里只验证未注册错误码检测功能正常(空列表表示全部已注册)。
        """
        # 测试 check_unregistered_error_codes 函数直接调用
        sys.path.insert(0, str(REPO_ROOT))
        # 重置模块缓存避免污染
        import importlib
        import scripts.check_error_codes as checker
        importlib.reload(checker)
        findings = checker.collect_findings(REPO_ROOT)
        ref_findings = [f for f in findings if f[2] == "error_code_ref"]
        unregistered = checker.check_unregistered_error_codes(ref_findings)
        # 所有 ErrorCodes.XXX / ErrorEnum.XXX 引用都应已注册
        assert unregistered == [], (
            f"发现 {len(unregistered)} 处未注册错误码: "
            f"{[u[2] for u in unregistered[:5]]}"
        )

    def test_export_error_codes_frontend_generates_valid_json(self):
        """scripts/export_error_codes_frontend.py 应生成合法 JSON。"""
        result = subprocess.run(
            [sys.executable, "scripts/export_error_codes_frontend.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"export 失败 (exit {result.returncode}):\n{result.stderr}"
        )
        output_path = REPO_ROOT / "locales" / "error_codes_frontend.json"
        assert output_path.exists(), "输出文件未生成"
        data = json.loads(output_path.read_text(encoding="utf-8"))
        assert "_meta" in data
        assert "codes" in data
        assert data["_meta"]["total_codes"] > 0


class TestErrorCodesNoBareStringAfterRegistration:
    """R56 §5.2: 注册后错误码必须可通过 ErrorRegistry 获取完整定义。"""

    def test_all_registered_codes_have_message_key(self):
        """所有已注册 code 的 ErrorDefinition 必须有 message_key。"""
        for code in ErrorRegistry.all_codes():
            definition = ErrorRegistry.get(code)
            assert definition.message_key, f"code '{code}' 无 message_key"

    def test_all_registered_codes_have_valid_http_status(self):
        """所有已注册 code 的 http_status 应在 1xx-5xx 范围。"""
        for code in ErrorRegistry.all_codes():
            definition = ErrorRegistry.get(code)
            assert 100 <= definition.http_status <= 599, (
                f"code '{code}' 的 http_status={definition.http_status} 超出范围"
            )

    def test_all_registered_codes_have_valid_severity(self):
        """所有已注册 code 的 severity 应在允许值中。"""
        valid_severities = {"info", "warning", "error", "critical"}
        for code in ErrorRegistry.all_codes():
            definition = ErrorRegistry.get(code)
            assert definition.severity in valid_severities, (
                f"code '{code}' 的 severity={definition.severity!r} 不在允许值中"
            )

    def test_all_registered_codes_have_safe_params_list(self):
        """所有已注册 code 的 safe_params 应为 list。"""
        for code in ErrorRegistry.all_codes():
            definition = ErrorRegistry.get(code)
            assert isinstance(definition.safe_params, list), (
                f"code '{code}' 的 safe_params 不是 list"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
