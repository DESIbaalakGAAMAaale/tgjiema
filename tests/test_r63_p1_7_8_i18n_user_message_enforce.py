"""R63 P1-07 / P1-08: report 按钮结果 i18n + UserMessage 真正强制 + 深层脱敏/不可变性。

P1-07 整改:
    - _handle_report_action 不再直接拼接"已提交封禁审批" / "已封禁用户" /
      "封禁失败" 等中文裸字符串。
    - 所有用户出口文案通过 ``UserMessage.from_key(...)`` + i18n key。
    - 中文与英文 locale 提供同构 ICU 参数。

P1-08 整改:
    - UserMessage.params 递归冻结(dict → MappingProxyType,list → tuple,
      deep copy 后转换;嵌套对象不可被外部修改)。
    - 敏感值过滤增强:不仅按 key 子串,还按 value 模式
      (API key 前缀 ghp_/sk-/AKIA/xoxb-、长 hex 字符串 >32 chars、JWT eyJ 前缀)。
    - 新增 ``ErrorEnvelope`` 类型(frozen dataclass,封装 AppError,
      提供 ``to_user_message(locale) -> UserMessage`` 方法)。
    - 新增 ``render_for_send(payload, i18n_manager)`` 适配器:
      类型级强制只接受 ``UserMessage | ErrorEnvelope``,拒绝裸 str,
      render 后立即发送(渲染集中在最后一层)。

测试组织:
    - ``TestUserMessageParamsRecursiveFreeze``     — params 递归冻结
    - ``TestUserMessageSensitiveValueDetection``   — value 模式脱敏
    - ``TestErrorEnvelopeWrapper``                 — AppError 封装/转换
    - ``TestRenderForSendAdapter``                — 类型级强制(拒绝 str)
    - ``TestCallbackReportActionI18n``            — callback.py 无硬编码中文
    - ``TestI18nKeysExistAndIsomorphic``          — zh-CN/en-US 同构 ICU 参数
"""
from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from types import MappingProxyType

import pytest

# 被测对象
from services.user_message import (
    UserMessage,
    ErrorEnvelope,
    render_for_send,
    _is_sensitive_value,
    _freeze_params,
)


# ════════════════════════════════════════════════════════════════
# 1. P1-08: UserMessage.params 递归冻结
# ════════════════════════════════════════════════════════════════


class TestUserMessageParamsRecursiveFreeze:
    """R63 P1-08: params 递归冻结(dict → MappingProxyType,list → tuple)。"""

    def test_top_level_dict_becomes_mapping_proxy(self):
        """UserMessage.params 顶层 dict 在构造后被替换为 MappingProxyType(只读)。"""
        msg = UserMessage.from_key("bot.upload_banned", params={"a": 1, "b": 2})
        # 顶层应是 MappingProxyType(不可变视图)
        assert isinstance(msg.params, MappingProxyType), (
            f"params 顶层应为 MappingProxyType,实际 {type(msg.params).__name__}"
        )
        # 内容等价于原 dict
        assert msg.params == {"a": 1, "b": 2}

    def test_nested_dict_becomes_mapping_proxy(self):
        """嵌套 dict 同样被递归冻结为 MappingProxyType。"""
        msg = UserMessage.from_key(
            "bot.upload_banned",
            params={"outer": {"inner": "value", "n": 1}},
        )
        assert isinstance(msg.params, MappingProxyType)
        assert isinstance(msg.params["outer"], MappingProxyType), (
            f"嵌套 dict 应为 MappingProxyType,实际 {type(msg.params['outer']).__name__}"
        )
        assert msg.params["outer"] == {"inner": "value", "n": 1}

    def test_list_becomes_tuple(self):
        """list 被递归冻结为 tuple(不可变)。"""
        msg = UserMessage.from_key(
            "bot.upload_banned",
            params={"items": [1, 2, 3], "tags": ["a", "b"]},
        )
        assert isinstance(msg.params, MappingProxyType)
        assert isinstance(msg.params["items"], tuple), (
            f"list 应被冻结为 tuple,实际 {type(msg.params['items']).__name__}"
        )
        assert msg.params["items"] == (1, 2, 3)
        assert msg.params["tags"] == ("a", "b")

    def test_nested_list_of_dicts_recursively_frozen(self):
        """list[dict, ...] 内的 dict 也被递归冻结。"""
        msg = UserMessage.from_key(
            "bot.upload_banned",
            params={"rows": [{"k": "v1"}, {"k": "v2"}]},
        )
        assert isinstance(msg.params["rows"], tuple)
        assert len(msg.params["rows"]) == 2
        for item in msg.params["rows"]:
            assert isinstance(item, MappingProxyType), (
                f"list 内 dict 应为 MappingProxyType,实际 {type(item).__name__}"
            )
        assert msg.params["rows"][0]["k"] == "v1"
        assert msg.params["rows"][1]["k"] == "v2"

    def test_top_level_dict_mutation_raises(self):
        """MappingProxyType 不允许直接修改键值(TypeError)。"""
        msg = UserMessage.from_key("bot.upload_banned", params={"a": 1})
        with pytest.raises(TypeError):
            msg.params["a"] = 999  # type: ignore[index]
        with pytest.raises(TypeError):
            msg.params["new_key"] = "x"  # type: ignore[index]
        with pytest.raises(TypeError):
            del msg.params["a"]  # type: ignore[misc]

    def test_nested_dict_mutation_raises(self):
        """嵌套 MappingProxyType 同样不可修改。"""
        msg = UserMessage.from_key(
            "bot.upload_banned",
            params={"outer": {"inner": "v"}},
        )
        with pytest.raises(TypeError):
            msg.params["outer"]["inner"] = "modified"  # type: ignore[index]

    def test_original_dict_mutation_does_not_propagate(self):
        """构造后修改原 dict 不影响 UserMessage(deep copy 隔离)。"""
        original = {"file_code": "ABC", "nested": {"k": "v"}}
        msg = UserMessage.from_key("bot.upload_banned", params=original)
        # 修改原始 dict 的顶层与嵌套
        original["file_code"] = "MODIFIED"
        original["nested"]["k"] = "HACKED"
        original["new_key"] = "new"
        # UserMessage.params 应保持原值(deep copy 隔离)
        assert msg.params["file_code"] == "ABC"
        assert msg.params["nested"]["k"] == "v"
        assert "new_key" not in msg.params

    def test_original_list_mutation_does_not_propagate(self):
        """构造后修改原 list 不影响 UserMessage(deep copy 隔离)。"""
        original = {"items": [1, 2, {"k": "v"}]}
        msg = UserMessage.from_key("bot.upload_banned", params=original)
        original["items"].append(999)
        original["items"][2]["k"] = "HACKED"
        # UserMessage.params["items"] 已被冻结为 tuple,不受原 list 修改影响
        assert msg.params["items"] == (1, 2, {"k": "v"})
        assert msg.params["items"][2] == {"k": "v"}

    def test_scalar_values_unchanged(self):
        """标量(int/str/bool/None-已过滤)保持不变。"""
        msg = UserMessage.from_key(
            "bot.upload_banned",
            params={"count": 5, "name": "abc", "flag": True},
        )
        assert msg.params["count"] == 5
        assert msg.params["name"] == "abc"
        assert msg.params["flag"] is True

    def test_freeze_params_helper_dict(self):
        """_freeze_params 辅助函数:dict → MappingProxyType。"""
        result = _freeze_params({"a": 1, "b": {"c": 2}})
        assert isinstance(result, MappingProxyType)
        assert isinstance(result["b"], MappingProxyType)
        assert result["b"]["c"] == 2

    def test_freeze_params_helper_list(self):
        """_freeze_params 辅助函数:list → tuple(递归)。"""
        result = _freeze_params([1, [2, 3], {"k": "v"}])
        assert isinstance(result, tuple)
        assert isinstance(result[1], tuple)
        assert isinstance(result[2], MappingProxyType)
        assert result == (1, (2, 3), {"k": "v"})

    def test_freeze_params_helper_scalar(self):
        """_freeze_params 辅助函数:标量保持不变。"""
        assert _freeze_params(42) == 42
        assert _freeze_params("hello") == "hello"
        assert _freeze_params(True) is True

    def test_freeze_params_helper_empty(self):
        """_freeze_params 辅助函数:空 dict/空 list。"""
        assert isinstance(_freeze_params({}), MappingProxyType)
        assert _freeze_params({}) == {}
        assert _freeze_params([]) == ()
        assert _freeze_params(()) == ()


# ════════════════════════════════════════════════════════════════
# 2. P1-08: value 模式脱敏(API key / 长 hex / JWT)
# ════════════════════════════════════════════════════════════════


class TestUserMessageSensitiveValueDetection:
    """R63 P1-08: 敏感值过滤增强 — 不仅按 key 子串,还按 value 模式。"""

    def test_github_token_prefix_detected(self):
        """ghp_ 前缀(GitHub PAT)被识别为敏感值。"""
        assert _is_sensitive_value("ghp_abcdef1234567890abcdef1234567890abcd") is True
        assert _is_sensitive_value("ghp_" + "x" * 36) is True

    def test_openai_api_key_prefix_detected(self):
        """sk- 前缀(OpenAI API key)被识别为敏感值。"""
        assert _is_sensitive_value("sk-abcdef1234567890abcdef1234567890") is True
        assert _is_sensitive_value("sk-" + "x" * 40) is True

    def test_aws_access_key_prefix_detected(self):
        """AKIA 前缀(AWS access key ID)被识别为敏感值。"""
        assert _is_sensitive_value("AKIAIOSFODNN7EXAMPLE") is True
        assert _is_sensitive_value("AKIA" + "X" * 16) is True

    def test_slack_token_prefix_detected(self):
        """xoxb- 前缀(Slack bot token)被识别为敏感值。"""
        assert _is_sensitive_value("xoxb-1234567890-abcdef") is True
        assert _is_sensitive_value("xoxb-" + "x" * 50) is True

    def test_long_hex_string_detected(self):
        """长 hex 字符串(>32 chars)被识别为敏感(可能是 token/hash)。"""
        # 32 字符 hex 刚好不触发(<=32 不敏感,边界)
        assert _is_sensitive_value("a" * 32) is False
        # 33 字符 hex 触发
        assert _is_sensitive_value("a" * 33) is True
        # 实际 hex token 示例(64 字符)
        assert _is_sensitive_value("abcdef0123456789" * 4) is True

    def test_jwt_prefix_detected(self):
        """eyJ 前缀(JWT header base64)被识别为敏感值。"""
        assert _is_sensitive_value("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig") is True
        assert _is_sensitive_value("eyJ" + "x" * 50) is True

    def test_normal_value_not_flagged(self):
        """正常 value 不被误判为敏感。"""
        assert _is_sensitive_value("ABC123") is False  # file_code
        assert _is_sensitive_value("user-abc-123") is False
        assert _is_sensitive_value("normal_message_text") is False
        assert _is_sensitive_value(42) is False  # 非 str
        assert _is_sensitive_value("a" * 20) is False  # 短字符串

    def test_user_message_filters_value_pattern_in_construction(self):
        """UserMessage 构造时按 value 模式过滤敏感值(即使 key 不敏感)。"""
        msg = UserMessage.from_key(
            "bot.upload_banned",
            params={
                "file_code": "ABC",  # 保留(非敏感)
                "user_token": "ghp_" + "x" * 36,  # 过滤(ghp_ 前缀)
                "api_data": "sk-" + "x" * 40,  # 过滤(sk- 前缀)
                "raw_value": "AKIAIOSFODNN7EXAMPLE",  # 过滤(AKIA 前缀)
                "hex_blob": "a" * 64,  # 过滤(长 hex)
                "auth_header": "eyJ" + "x" * 50,  # 过滤(JWT)
            },
        )
        # 敏感值被过滤
        assert "user_token" not in msg.params
        assert "api_data" not in msg.params
        assert "raw_value" not in msg.params
        assert "hex_blob" not in msg.params
        assert "auth_header" not in msg.params
        # 正常值保留
        assert msg.params["file_code"] == "ABC"

    def test_user_message_filters_key_substring_still_works(self):
        """R62 行为保持:key 子串过滤(password/secret/token 等)仍生效。"""
        msg = UserMessage.from_key(
            "bot.upload_banned",
            params={
                "file_code": "ABC",
                "password": "any_value",  # key 含 password → 过滤
                "user_secret": "v",  # key 含 secret → 过滤
            },
        )
        assert "password" not in msg.params
        assert "user_secret" not in msg.params
        assert msg.params["file_code"] == "ABC"


# ════════════════════════════════════════════════════════════════
# 3. P1-08: ErrorEnvelope 封装 AppError → UserMessage
# ════════════════════════════════════════════════════════════════


class TestErrorEnvelopeWrapper:
    """R63 P1-08: ErrorEnvelope — 封装 AppError,提供 to_user_message(locale)。"""

    def test_error_envelope_is_frozen_dataclass(self):
        """ErrorEnvelope 应为 frozen dataclass。"""
        from services.error_codes import AppError, ErrorCodes
        app_error = AppError(ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT, params={"file_code": "abc"})
        env = ErrorEnvelope(app_error)
        assert dataclasses.is_dataclass(env)
        params = getattr(ErrorEnvelope, "__dataclass_params__", None)
        assert params is not None
        assert params.frozen is True, "ErrorEnvelope 应为 frozen=True"

    def test_error_envelope_cannot_reassign_app_error(self):
        """frozen ErrorEnvelope 不允许重新赋值 app_error 字段。"""
        from services.error_codes import AppError, ErrorCodes
        env = ErrorEnvelope(AppError(ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT))
        with pytest.raises(dataclasses.FrozenInstanceError):
            env.app_error = AppError(ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT)  # type: ignore[misc]

    def test_to_user_message_produces_user_message(self):
        """ErrorEnvelope.to_user_message() 返回 UserMessage(字段对齐 AppError)。"""
        from services.error_codes import AppError, ErrorCodes
        app_error = AppError(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "FILE_XYZ"},
        )
        env = ErrorEnvelope(app_error)
        msg = env.to_user_message(locale="zh-CN")
        assert isinstance(msg, UserMessage)
        # message_key 来自 AppError.envelope
        assert msg.message_key == app_error.envelope.message_key
        # error_code 透传
        assert msg.error_code == app_error.code
        # trace_id 全链路关联
        assert msg.trace_id == app_error.trace_id
        # locale 正确传递
        assert msg.locale == "zh-CN"

    def test_to_user_message_default_locale_zh_cn(self):
        """to_user_message() 默认 locale=zh-CN。"""
        from services.error_codes import AppError, ErrorCodes
        env = ErrorEnvelope(AppError(ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT))
        msg = env.to_user_message()
        assert msg.locale == "zh-CN"

    def test_to_user_message_en_us_locale(self):
        """to_user_message(locale='en-US') 切换 locale。"""
        from services.error_codes import AppError, ErrorCodes
        env = ErrorEnvelope(AppError(ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT))
        msg = env.to_user_message(locale="en-US")
        assert msg.locale == "en-US"

    def test_render_via_envelope_produces_localized_text(self):
        """ErrorEnvelope 通过 render_for_send 渲染为本地化字符串(含 file_code)。"""
        from services.i18n import get_i18n_manager
        from services.error_codes import AppError, ErrorCodes
        manager = get_i18n_manager()
        app_error = AppError(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "FILE_R63"},
        )
        env = ErrorEnvelope(app_error)
        text = render_for_send(env, manager)
        assert "FILE_R63" in text, f"渲染应含 file_code=FILE_R63: {text}"

    def test_envelope_to_user_message_params_frozen(self):
        """ErrorEnvelope.to_user_message() 返回的 UserMessage params 已递归冻结。"""
        from services.error_codes import AppError, ErrorCodes
        env = ErrorEnvelope(AppError(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "ABC"},
        ))
        msg = env.to_user_message()
        assert isinstance(msg.params, MappingProxyType)


# ════════════════════════════════════════════════════════════════
# 4. P1-08: render_for_send 适配器 — 类型级强制
# ════════════════════════════════════════════════════════════════


class TestRenderForSendAdapter:
    """R63 P1-08: render_for_send(payload, i18n_manager) — 类型级强制。

    adapter 签名接收 UserMessage | ErrorEnvelope,禁止 str。
    渲染集中在 adapter 最后一层(render 后立即发送)。
    """

    def test_rejects_bare_str(self):
        """裸 str 应被拒绝(TypeError) — 强制使用 UserMessage.from_key()。"""
        from services.i18n import get_i18n_manager
        manager = get_i18n_manager()
        with pytest.raises(TypeError, match=r"不接受裸 str"):
            render_for_send("hardcoded string", manager)  # type: ignore[arg-type]

    def test_rejects_bare_str_with_meaningful_message(self):
        """TypeError 信息明确指出整改路径(UserMessage.from_key / ErrorEnvelope)。"""
        from services.i18n import get_i18n_manager
        manager = get_i18n_manager()
        with pytest.raises(TypeError) as exc_info:
            render_for_send("中文裸字符串", manager)  # type: ignore[arg-type]
        msg = str(exc_info.value)
        assert "UserMessage" in msg or "ErrorEnvelope" in msg, (
            f"TypeError 信息应提到 UserMessage/ErrorEnvelope: {msg}"
        )

    def test_accepts_user_message(self):
        """UserMessage 通过 render_for_send 渲染为本地化字符串。"""
        from services.i18n import get_i18n_manager
        manager = get_i18n_manager()
        msg = UserMessage.from_key(
            "bot.quota_remaining",
            locale="zh-CN",
            params={"count": 5},
        )
        text = render_for_send(msg, manager)
        assert "5" in text
        assert "今日剩余配额" in text

    def test_accepts_error_envelope(self):
        """ErrorEnvelope 通过 render_for_send 渲染(等价于 to_user_message().render())。"""
        from services.i18n import get_i18n_manager
        from services.error_codes import AppError, ErrorCodes
        manager = get_i18n_manager()
        env = ErrorEnvelope(AppError(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "FC"},
        ))
        text = render_for_send(env, manager)
        assert "FC" in text

    def test_rejects_other_types(self):
        """非 UserMessage/ErrorEnvelope/str 的类型也应被拒绝。"""
        from services.i18n import get_i18n_manager
        manager = get_i18n_manager()
        with pytest.raises(TypeError):
            render_for_send(123, manager)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            render_for_send(None, manager)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            render_for_send({"msg": "x"}, manager)  # type: ignore[arg-type]

    def test_render_for_send_returns_str(self):
        """render_for_send 返回值为 str(供 sink 直接使用)。"""
        from services.i18n import get_i18n_manager
        manager = get_i18n_manager()
        msg = UserMessage.from_key("bot.upload_banned", locale="zh-CN")
        text = render_for_send(msg, manager)
        assert isinstance(text, str)


# ════════════════════════════════════════════════════════════════
# 5. P1-07: callback.py _handle_report_action 不再含硬编码中文
# ════════════════════════════════════════════════════════════════


class TestCallbackReportActionI18n:
    """R63 P1-07: _handle_report_action 所有用户出口文案通过 i18n key / UserMessage。"""

    # callback.py 绝对路径
    CALLBACK_PATH = Path(__file__).resolve().parent.parent / "bots" / "admin_bot" / "callback.py"

    # P1-07 明确禁止的硬编码中文裸字符串(原 f-string 拼接)
    FORBIDDEN_LITERALS = [
        "已提交封禁审批",
        "已封禁用户",
        "封禁失败",
        "已提交审批",  # detach / block 的 "已提交审批"
        "已脱钩文件码",
        "脱钩失败",
        "已限制举报人",
        "限制失败",
        "操作失败",  # query.answer("操作失败: ...")
    ]

    def test_callback_file_exists(self):
        """前置条件:callback.py 文件存在。"""
        assert self.CALLBACK_PATH.exists(), f"callback.py 不存在: {self.CALLBACK_PATH}"

    def test_no_forbidden_chinese_literals_in_callback(self):
        """callback.py 不应再含 P1-07 列出的硬编码中文裸字符串。"""
        content = self.CALLBACK_PATH.read_text(encoding='utf-8')
        violations = []
        for literal in self.FORBIDDEN_LITERALS:
            if literal in content:
                violations.append(literal)
        assert not violations, (
            f"callback.py 仍含硬编码中文裸字符串: {violations}"
        )

    def test_no_chinese_fstring_concat_in_report_handler(self):
        """_handle_report_action 内不再有 `+ f"\\n\\n...中文..."` 模式。"""
        content = self.CALLBACK_PATH.read_text(encoding='utf-8')
        # 匹配 + f"...\n\n⏳... 或 + f"...\n\n✅... 或 + f"...\n\n❌...
        # (P1-07 原代码模式:query.message.text + f"\\n\\n<emoji><中文>")
        pattern = re.compile(r'\+\s*f["\'][^"\']*[\u4e00-\u9fff][^"\']*["\']')
        # 仅检查 _handle_report_action 函数体内
        match = re.search(
            r'async def _handle_report_action.*?(?=\nasync def |\nclass |\Z)',
            content,
            re.DOTALL,
        )
        assert match is not None, "未找到 _handle_report_action 函数"
        func_body = match.group(0)
        bad = pattern.findall(func_body)
        assert not bad, (
            f"_handle_report_action 内仍含 + f\"中文...\" 拼接: {bad}"
        )

    def test_report_handler_imports_user_message(self):
        """callback.py 应导入 UserMessage / render_for_send(类型强制)。"""
        content = self.CALLBACK_PATH.read_text(encoding='utf-8')
        assert "from services.user_message import" in content, (
            "callback.py 应导入 services.user_message 中的符号"
        )
        assert "UserMessage" in content, "callback.py 应使用 UserMessage"

    def test_report_uses_user_message_from_key_pattern(self):
        """_handle_report_action 内应使用 UserMessage.from_key(...) 模式。"""
        content = self.CALLBACK_PATH.read_text(encoding='utf-8')
        match = re.search(
            r'async def _handle_report_action.*?(?=\nasync def |\nclass |\Z)',
            content,
            re.DOTALL,
        )
        assert match is not None
        func_body = match.group(0)
        # 应至少出现一次 UserMessage.from_key
        assert "UserMessage.from_key" in func_body or "_i18n_t(" in func_body, (
            "_handle_report_action 应通过 UserMessage.from_key 或 _i18n_t 出口"
        )


# ════════════════════════════════════════════════════════════════
# 6. P1-07: i18n key 在 zh-CN / en-US 中存在且 ICU 参数同构
# ════════════════════════════════════════════════════════════════


class TestI18nKeysExistAndIsomorphic:
    """R63 P1-07: 新增 admin.callback.button_security.* key 在 zh-CN/en-US 中存在,
    且 ICU 参数(占位符)同构(双语提供相同变量集)。"""

    LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"

    # P1-07 新增的 i18n key(覆盖 ban/detach/block 三种 sub_action ×
    # 三种结果 approval_required/success/error + operation_failed)
    NEW_KEYS = [
        "admin.callback.button_security.ban_approval_submitted",
        "admin.callback.button_security.ban_success",
        "admin.callback.button_security.ban_failed",
        "admin.callback.button_security.detach_approval_submitted",
        "admin.callback.button_security.detach_success",
        "admin.callback.button_security.detach_failed",
        "admin.callback.button_security.block_approval_submitted",
        "admin.callback.button_security.block_success",
        "admin.callback.button_security.block_failed",
        "admin.callback.button_security.operation_failed",
    ]

    def _load_flat(self, locale: str) -> dict:
        """加载 locale JSON 并扁平化为点分 key → value dict。"""
        path = self.LOCALES_DIR / f"{locale}.json"
        data = json.loads(path.read_text(encoding='utf-8'))
        flat: dict[str, str] = {}

        def _flatten(obj, prefix):
            for k, v in obj.items():
                full = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    _flatten(v, full)
                elif isinstance(v, str):
                    flat[full] = v
                else:
                    flat[full] = str(v)

        _flatten(data, "")
        return flat

    @pytest.mark.parametrize("key", NEW_KEYS)
    def test_key_exists_in_zh_cn(self, key):
        """新增 key 存在于 zh-CN.json。"""
        flat = self._load_flat("zh-CN")
        assert key in flat, f"zh-CN.json 缺失 key: {key}"

    @pytest.mark.parametrize("key", NEW_KEYS)
    def test_key_exists_in_en_us(self, key):
        """新增 key 存在于 en-US.json。"""
        flat = self._load_flat("en-US")
        assert key in flat, f"en-US.json 缺失 key: {key}"

    @pytest.mark.parametrize("key", NEW_KEYS)
    def test_key_icu_params_isomorphic(self, key):
        """同一 key 在 zh-CN/en-US 中 ICU 占位符集合相同(同构参数)。"""
        zh = self._load_flat("zh-CN")[key]
        en = self._load_flat("en-US")[key]
        zh_params = set(re.findall(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}', zh))
        en_params = set(re.findall(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}', en))
        assert zh_params == en_params, (
            f"key={key} ICU 参数不同构: zh-CN={zh_params}, en-US={en_params}\n"
            f"  zh-CN: {zh!r}\n  en-US: {en!r}"
        )

    @pytest.mark.parametrize("key", NEW_KEYS)
    def test_zh_cn_value_contains_chinese(self, key):
        """zh-CN 翻译应含中文字符(避免误填英文)。"""
        flat = self._load_flat("zh-CN")
        assert re.search(r'[\u4e00-\u9fff]', flat[key]), (
            f"zh-CN.json key={key} 应含中文: {flat[key]!r}"
        )

    @pytest.mark.parametrize("key", NEW_KEYS)
    def test_en_us_value_contains_english(self, key):
        """en-US 翻译应含 ASCII 字母(避免误填中文)。"""
        flat = self._load_flat("en-US")
        assert re.search(r'[a-zA-Z]', flat[key]), (
            f"en-US.json key={key} 应含英文: {flat[key]!r}"
        )


# ════════════════════════════════════════════════════════════════
# 7. 兼容性:R62 既有 UserMessage 行为保持
# ════════════════════════════════════════════════════════════════


class TestR62BackwardCompatibility:
    """R62 P1-05 既有行为在 R63 P1-08 增强后保持兼容。"""

    def test_user_message_still_frozen(self):
        """UserMessage 仍为 frozen dataclass。"""
        assert dataclasses.is_dataclass(UserMessage)
        params = getattr(UserMessage, "__dataclass_params__", None)
        assert params is not None
        assert params.frozen is True

    def test_from_key_with_no_params_still_works(self):
        """from_key() 不传 params 时 params 为空(且为 MappingProxyType)。"""
        msg = UserMessage.from_key("bot.upload_banned")
        assert msg.params == {}
        assert isinstance(msg.params, MappingProxyType)

    def test_render_still_produces_localized_text(self):
        """render() 仍可通过 i18n_manager 渲染本地化字符串。"""
        from services.i18n import get_i18n_manager
        manager = get_i18n_manager()
        msg = UserMessage.from_key(
            "bot.quota_remaining",
            locale="zh-CN",
            params={"count": 3},
        )
        text = msg.render(manager)
        assert "3" in text
        assert "今日剩余配额" in text

    def test_from_error_still_works(self):
        """from_error() 仍可从 AppError 构造 UserMessage。"""
        from services.error_codes import AppError, ErrorCodes
        app_error = AppError(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "FC"},
        )
        msg = UserMessage.from_error(app_error, locale="zh-CN")
        assert msg.message_key == app_error.envelope.message_key
        assert msg.error_code == app_error.code
        assert msg.trace_id == app_error.trace_id

    def test_sensitive_key_substring_filter_still_works(self):
        """key 子串敏感过滤(password/secret/token 等)仍生效。"""
        msg = UserMessage.from_key(
            "bot.upload_banned",
            params={
                "file_code": "ABC",
                "password": "any",
                "api_token": "any",
                "secret_field": "any",
            },
        )
        assert "password" not in msg.params
        assert "api_token" not in msg.params
        assert "secret_field" not in msg.params
        assert msg.params["file_code"] == "ABC"

    def test_none_value_filtered_still_works(self):
        """None 值过滤(避免 'None' 字面量泄漏)仍生效。"""
        msg = UserMessage.from_key(
            "bot.upload_banned",
            params={"file_code": "ABC", "user_id": None, "channel_id": None},
        )
        assert "user_id" not in msg.params
        assert "channel_id" not in msg.params
        assert msg.params["file_code"] == "ABC"
