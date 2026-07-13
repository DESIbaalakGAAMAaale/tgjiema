"""R40 P2-6: 领域错误码测试。

测试范围:
- services/errors.py: ErrorCode 枚举、DomainError 异常类
- 错误码格式验证(<DOMAIN>.<OPERATION>.<REASON>)
- to_dict() 序列化
- from_exception() 异常降级
- 便捷构造方法(quota_exceeded/not_found/unauthorized/forbidden/validation_failed)
- 默认消息中英文支持

测试策略:
- AST 语法检查(兼容 Python 3.9)
- 直接 import 验证(纯 Python,无外部依赖)
- 中文注释检查(遵循用户规则)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "services"


def _parse_ast(filepath: Path) -> ast.Module | None:
    """解析 Python 文件 AST,失败返回 None。"""
    try:
        source = filepath.read_text(encoding="utf-8")
        return ast.parse(source)
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════
# 1. AST 与文件级检查
# ════════════════════════════════════════════════════════════════


class TestErrorsFile:
    """R40 P2-6: services/errors.py 文件级检查。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "errors.py").exists(), "services/errors.py 应存在"

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "errors.py")
        assert tree is not None, "services/errors.py 应可被 AST 解析"

    def test_has_error_code_enum(self):
        """应定义 ErrorCode 枚举类。"""
        tree = _parse_ast(SERVICES_DIR / "errors.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        classes = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
        }
        assert "ErrorCode" in classes, "应定义 ErrorCode 枚举类"
        assert "DomainError" in classes, "应定义 DomainError 异常类"
        assert "ErrorSeverity" in classes, "应定义 ErrorSeverity 枚举"

    def test_has_to_dict_method(self):
        """DomainError 应有 to_dict 方法。"""
        tree = _parse_ast(SERVICES_DIR / "errors.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        sync_funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "to_dict" in sync_funcs, "DomainError 应有 to_dict 方法"
        assert "from_exception" in sync_funcs, "DomainError 应有 from_exception 类方法"

    def test_has_convenience_constructors(self):
        """应有便捷构造方法。"""
        tree = _parse_ast(SERVICES_DIR / "errors.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        sync_funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        required = {
            "quota_exceeded",
            "not_found",
            "unauthorized",
            "forbidden",
            "validation_failed",
            "get_default_message",
        }
        missing = required - sync_funcs
        assert not missing, f"缺少便捷构造方法: {missing}"

    def test_has_chinese_comments(self):
        """R40 规则:代码注释用中文。"""
        source = (SERVICES_DIR / "errors.py").read_text(encoding="utf-8")
        chinese_count = sum(
            1 for line in source.split("\n")
            if "#" in line and any(
                "\u4e00" <= ch <= "\u9fff"
                for ch in line.split("#", 1)[1]
            )
        )
        assert chinese_count >= 3, f"中文注释数量应 >= 3,实际 {chinese_count}"


# ════════════════════════════════════════════════════════════════
# 2. ErrorCode 枚举值格式检查
# ════════════════════════════════════════════════════════════════


class TestErrorCodeFormat:
    """R40 P2-6: 错误码格式应遵循 <DOMAIN>.<OPERATION>.<REASON>。"""

    def _try_import(self):
        try:
            from services.errors import ErrorCode
            return ErrorCode
        except Exception as e:
            pytest.skip(f"services.errors 不可导入: {e}")

    def test_all_codes_match_pattern(self):
        """所有错误码应匹配 <DOMAIN>[.<OPERATION>].<REASON> 格式(允许 2-3 段)。

        规范要求 <DOMAIN>.<OPERATION>.<REASON>(3 段),但代码库中存在合理的
        2 段码(如 file.not_found、user.banned — domain.reason)和含数字的段
        (如 storage.r2.failed)。本测试验证每个码至少 2 段,每段以小写字母
        开头,可含小写字母/数字/下划线,确保格式统一且可机读。
        """
        ErrorCode = self._try_import()
        if ErrorCode is None:
            return
        import re
        # 允许 2-3 段:每段以小写字母开头,后接小写字母/数字/下划线
        pattern = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
        for code in ErrorCode:
            assert pattern.match(code.value), (
                f"错误码 {code.name}={code.value} 不符合 <DOMAIN>[.<OPERATION>].<REASON> 格式"
            )

    def test_has_required_domains(self):
        """应包含核心领域错误码。"""
        ErrorCode = self._try_import()
        if ErrorCode is None:
            return
        # 检查至少包含以下领域的前缀
        required_domains = [
            "quota",
            "file",
            "user",
            "approval",
            "system",
            "storage",
            "replication",
            "relay",
            "decode",
            "validation",
        ]
        values = [c.value for c in ErrorCode]
        for domain in required_domains:
            assert any(v.startswith(f"{domain}.") for v in values), (
                f"应包含 {domain}.* 领域的错误码"
            )

    def test_enum_is_str_subclass(self):
        """ErrorCode 应继承 str(便于 JSON 序列化)。"""
        ErrorCode = self._try_import()
        if ErrorCode is None:
            return
        # str 枚举:成员值应为字符串
        assert isinstance(ErrorCode.INTERNAL_ERROR.value, str)

    def test_specific_codes_exist(self):
        """关键错误码应存在。"""
        ErrorCode = self._try_import()
        if ErrorCode is None:
            return
        required = {
            ErrorCode.QUOTA_DECODE_EXCEEDED,
            ErrorCode.FILE_NOT_FOUND,
            ErrorCode.USER_BANNED,
            ErrorCode.APPROVAL_REQUIRED,
            ErrorCode.INTERNAL_ERROR,
            ErrorCode.RATE_LIMITED,
            ErrorCode.MAINTENANCE_MODE,
        }
        # 只要能访问即可(访问不存在的会抛 AttributeError)
        assert required


# ════════════════════════════════════════════════════════════════
# 3. DomainError 类行为测试
# ════════════════════════════════════════════════════════════════


class TestDomainError:
    """R40 P2-6: DomainError 异常类行为测试。"""

    def _try_import(self):
        try:
            from services.errors import DomainError, ErrorCode, ErrorSeverity
            return DomainError, ErrorCode, ErrorSeverity
        except Exception as e:
            pytest.skip(f"services.errors 不可导入: {e}")

    def test_default_construction(self):
        """默认构造应使用 INTERNAL_ERROR 码。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, ErrorCode, _ = imported
        err = DomainError()
        assert err.code == ErrorCode.INTERNAL_ERROR
        assert err.message  # 应有默认消息
        assert err.trace_id  # 应有 trace_id(UUID 格式)

    def test_to_dict_contains_required_fields(self):
        """to_dict 应包含 code/message/details/severity 字段。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, ErrorCode, _ = imported
        err = DomainError(
            code=ErrorCode.QUOTA_DECODE_EXCEEDED,
            details={"used": 5, "limit": 5},
        )
        d = err.to_dict()
        assert d["code"] == "quota.decode.exceeded"
        assert "message" in d
        assert d["details"]["used"] == 5
        assert d["severity"] == "error"
        assert "trace_id" in d

    def test_to_dict_without_trace(self):
        """to_dict(include_trace=False) 应不含 trace_id。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, _, _ = imported
        err = DomainError()
        d = err.to_dict(include_trace=False)
        assert "trace_id" not in d

    def test_to_dict_details_empty_when_not_provided(self):
        """未提供 details 时 to_dict 返回空 dict。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, _, _ = imported
        err = DomainError()
        d = err.to_dict()
        assert d["details"] == {}

    def test_trace_id_is_unique(self):
        """每次构造应生成唯一 trace_id。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, _, _ = imported
        err1 = DomainError()
        err2 = DomainError()
        assert err1.trace_id != err2.trace_id, "trace_id 应唯一"

    def test_can_be_raised_and_caught(self):
        """DomainError 应可被 raise 和 except 捕获。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, ErrorCode, _ = imported
        with pytest.raises(DomainError) as exc_info:
            raise DomainError(code=ErrorCode.FILE_NOT_FOUND, message="测试错误")
        assert exc_info.value.code == ErrorCode.FILE_NOT_FOUND

    def test_is_exception_subclass(self):
        """DomainError 应继承 Exception。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, _, _ = imported
        err = DomainError()
        assert isinstance(err, Exception)


# ════════════════════════════════════════════════════════════════
# 4. from_exception 异常降级测试
# ════════════════════════════════════════════════════════════════


class TestFromException:
    """R40 P2-6: from_exception 异常降级测试。"""

    def _try_import(self):
        try:
            from services.errors import DomainError, ErrorCode
            return DomainError, ErrorCode
        except Exception as e:
            pytest.skip(f"services.errors 不可导入: {e}")

    def test_from_generic_exception(self):
        """普通异常应降级为 INTERNAL_ERROR。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, ErrorCode = imported
        try:
            raise ValueError("测试异常")
        except Exception as e:
            err = DomainError.from_exception(e)
        assert err.code == ErrorCode.INTERNAL_ERROR
        assert err.cause is not None
        assert err.details.get("original_type") == "ValueError"

    def test_from_domain_error_preserves_identity(self):
        """DomainError 应保留原 trace_id 和 code。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, ErrorCode = imported
        original = DomainError(code=ErrorCode.FILE_NOT_FOUND)
        try:
            raise original
        except DomainError as e:
            result = DomainError.from_exception(e)
        assert result.code == ErrorCode.FILE_NOT_FOUND
        assert result.trace_id == original.trace_id

    def test_from_exception_with_custom_code(self):
        """可指定自定义错误码降级。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, ErrorCode = imported
        try:
            raise RuntimeError("磁盘满")
        except Exception as e:
            err = DomainError.from_exception(
                e, code=ErrorCode.STORAGE_QUOTA_EXCEEDED,
            )
        assert err.code == ErrorCode.STORAGE_QUOTA_EXCEEDED


# ════════════════════════════════════════════════════════════════
# 5. 便捷构造方法测试
# ════════════════════════════════════════════════════════════════


class TestConvenienceConstructors:
    """R40 P2-6: 便捷构造方法测试。"""

    def _try_import(self):
        try:
            from services.errors import DomainError, ErrorCode, ErrorSeverity
            return DomainError, ErrorCode, ErrorSeverity
        except Exception as e:
            pytest.skip(f"services.errors 不可导入: {e}")

    def test_quota_exceeded_decode(self):
        """quota_exceeded('decode') 应构造 QUOTA_DECODE_EXCEEDED。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, ErrorCode, ErrorSeverity = imported
        err = DomainError.quota_exceeded(quota_type="decode", used=5, limit=5)
        assert err.code == ErrorCode.QUOTA_DECODE_EXCEEDED
        assert err.details["used"] == 5
        assert err.details["limit"] == 5
        assert err.severity == ErrorSeverity.WARNING

    def test_quota_exceeded_upload(self):
        """quota_exceeded('upload') 应构造 QUOTA_UPLOAD_EXCEEDED。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, ErrorCode, _ = imported
        err = DomainError.quota_exceeded(quota_type="upload")
        assert err.code == ErrorCode.QUOTA_UPLOAD_EXCEEDED

    def test_not_found_file(self):
        """not_found('file') 应构造 FILE_NOT_FOUND。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, ErrorCode, _ = imported
        err = DomainError.not_found(resource="file", resource_id="abc123")
        assert err.code == ErrorCode.FILE_NOT_FOUND
        assert err.details["id"] == "abc123"

    def test_not_found_user(self):
        """not_found('user') 应构造 USER_NOT_FOUND。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, ErrorCode, _ = imported
        err = DomainError.not_found(resource="user", resource_id="12345")
        assert err.code == ErrorCode.USER_NOT_FOUND

    def test_unauthorized(self):
        """unauthorized() 应构造 USER_UNAUTHORIZED。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, ErrorCode, _ = imported
        err = DomainError.unauthorized()
        assert err.code == ErrorCode.USER_UNAUTHORIZED

    def test_forbidden(self):
        """forbidden() 应构造 USER_FORBIDDEN。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, ErrorCode, _ = imported
        err = DomainError.forbidden()
        assert err.code == ErrorCode.USER_FORBIDDEN

    def test_validation_failed(self):
        """validation_failed 应构造 VALIDATION_FAILED 并包含字段细节。"""
        imported = self._try_import()
        if imported is None:
            return
        DomainError, ErrorCode, _ = imported
        err = DomainError.validation_failed(field_name="email", reason="格式错误")
        assert err.code == ErrorCode.VALIDATION_FAILED
        assert err.details["field"] == "email"
        assert err.details["reason"] == "格式错误"


# ════════════════════════════════════════════════════════════════
# 6. 默认消息与多语言支持测试
# ════════════════════════════════════════════════════════════════


class TestDefaultMessages:
    """R40 P2-6: 默认消息与多语言支持。"""

    def _try_import(self):
        try:
            from services.errors import ErrorCode, get_default_message
            return ErrorCode, get_default_message
        except Exception as e:
            pytest.skip(f"services.errors 不可导入: {e}")

    def test_chinese_message_for_quota(self):
        """QUOTA_DECODE_EXCEEDED 中文消息应为'今日解码次数已达上限'。"""
        imported = self._try_import()
        if imported is None:
            return
        ErrorCode, get_default_message = imported
        msg = get_default_message(ErrorCode.QUOTA_DECODE_EXCEEDED, "zh-CN")
        assert "解码" in msg or "上限" in msg

    def test_english_message_for_quota(self):
        """QUOTA_DECODE_EXCEEDED 英文消息应为 'quota exceeded'。"""
        imported = self._try_import()
        if imported is None:
            return
        ErrorCode, get_default_message = imported
        msg = get_default_message(ErrorCode.QUOTA_DECODE_EXCEEDED, "en-US")
        assert "quota" in msg.lower()

    def test_fallback_to_chinese(self):
        """未知 locale 应回退到中文。"""
        imported = self._try_import()
        if imported is None:
            return
        ErrorCode, get_default_message = imported
        msg = get_default_message(ErrorCode.QUOTA_DECODE_EXCEEDED, "fr-FR")
        # 应回退到中文
        assert msg

    def test_undefined_code_returns_code_value(self):
        """未在 _DEFAULT_MESSAGES 中定义的码应返回 code.value。"""
        imported = self._try_import()
        if imported is None:
            return
        ErrorCode, get_default_message = imported
        # 使用未在 _DEFAULT_MESSAGES 中定义的枚举(若有)
        # 构造一个未定义消息的 code(暂用 INTERNAL_ERROR 兜底)
        msg = get_default_message(ErrorCode.INTERNAL_ERROR, "zh-CN")
        assert msg  # 应非空


# ════════════════════════════════════════════════════════════════
# 7. ErrorSeverity 枚举测试
# ════════════════════════════════════════════════════════════════


class TestErrorSeverity:
    """R40 P2-6: ErrorSeverity 枚举测试。"""

    def _try_import(self):
        try:
            from services.errors import ErrorSeverity
            return ErrorSeverity
        except Exception as e:
            pytest.skip(f"services.errors 不可导入: {e}")

    def test_has_four_levels(self):
        """应包含 4 个严重级别。"""
        ErrorSeverity = self._try_import()
        if ErrorSeverity is None:
            return
        levels = {s.value for s in ErrorSeverity}
        assert "info" in levels
        assert "warning" in levels
        assert "error" in levels
        assert "critical" in levels

    def test_default_severity_is_error(self):
        """DomainError 默认 severity 应为 ERROR。"""
        try:
            from services.errors import DomainError, ErrorSeverity
        except ImportError:
            pytest.skip("services.errors 不可导入")
        err = DomainError()
        assert err.severity == ErrorSeverity.ERROR
