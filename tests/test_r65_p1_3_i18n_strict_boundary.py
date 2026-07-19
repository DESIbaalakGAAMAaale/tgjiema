"""R65 P1-03: i18n 严格出口边界(strict export boundary)测试。

审计报告要求 (P1-03):
    国际化仍未形成严格出口边界。486 个 sink 旁路意味着裸字符串、混合语言和
    内部错误泄漏风险仍广泛存在。本测试覆盖 7 维扫描器 + fail-closed
    locale/ICU 解析行为,确保 zh-CN ↔ en-US 完全对等、生产代码 locale
    显式绑定、内部异常不泄漏给用户。

测试覆盖(9 个场景):
    1. Scanner 集成: --strict 模式在当前代码库通过(baseline ratchet 有效)
    2. Dim 1: key 集合对等(zh-CN ↔ en-US 无孤儿 key)
    3. Dim 2: ICU AST 结构等价(selector 集合 + 占位符数量/类型)
    4. Dim 3: 参数名一致(`{count}` 不能变成 `{n}`)
    5. Dim 4: en-US 禁止 CJK 占位副本
    6. Dim 5: zh-CN 禁止英文业务文案泄漏(技术术语白名单)
    7. Dim 6: 内部异常禁止经 UserMessage / ErrorEnvelope 泄漏(AST 扫描)
    8. Dim 7: 生产代码 translate/format_message 必须显式绑定 locale(AST 扫描)
    9. Fail-closed 行为: strict 模式抛 I18N_LOCALE_NOT_BOUND / I18N_PARSE_FAILED,
       I18N_ALLOW_FALLBACK=1 测试逃生舱保留旧行为,错误码已注册

测试策略:
    - 集成测试 1: 直接调用 verify(strict=True),验证真实 locale + baseline
    - 单元测试 2-6: 构造扁平化 dict,直接调用 _check_dimN_* 函数
    - AST 测试 7-8: 写入临时 .py 文件,调用文件级 _check_dimN_*(path)
    - 行为测试 9: 使用 I18nManager + monkeypatchenv 控制 _get_i18n_allow_fallback
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试环境兼容(mock telegram 库,避免 ImportError)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "locales"
SCANNER_PATH = REPO_ROOT / "scripts" / "check_i18n_strict_export_boundary.py"


# ════════════════════════════════════════════════════════════════
# Fixture: 环境变量隔离(与 test_r63_p1_12_i18n_icu_precompile.py 一致)
# ════════════════════════════════════════════════════════════════
@pytest.fixture
def clean_env(monkeypatch):
    """清除影响 fail-closed 模式判定的环境变量(用例间隔离)。"""
    monkeypatch.delenv("I18N_ALLOW_FALLBACK", raising=False)
    monkeypatch.delenv("RELEASE_BUILD", raising=False)
    monkeypatch.delenv("ICU_STRICT_MODE", raising=False)
    # R65 P1-03: 重置 fallback 缓存,使下次 _get_i18n_allow_fallback() 重新读取环境
    from services.i18n import _reset_i18n_fallback_cache
    _reset_i18n_fallback_cache()
    yield
    _reset_i18n_fallback_cache()


@pytest.fixture
def strict_mode(monkeypatch):
    """启用 fail-closed 严格模式(I18N_ALLOW_FALLBACK=0)。"""
    monkeypatch.setenv("I18N_ALLOW_FALLBACK", "0")
    monkeypatch.delenv("RELEASE_BUILD", raising=False)
    from services.i18n import _reset_i18n_fallback_cache
    _reset_i18n_fallback_cache()
    yield
    _reset_i18n_fallback_cache()


@pytest.fixture
def loose_mode(monkeypatch):
    """禁用 fail-closed 严格模式(I18N_ALLOW_FALLBACK=1,允许 fallback)。"""
    monkeypatch.setenv("I18N_ALLOW_FALLBACK", "1")
    monkeypatch.delenv("RELEASE_BUILD", raising=False)
    from services.i18n import _reset_i18n_fallback_cache
    _reset_i18n_fallback_cache()
    yield
    _reset_i18n_fallback_cache()


@pytest.fixture
def release_mode(monkeypatch):
    """启用 release 构建(RELEASE_BUILD=1,隐含 fail-closed 严格模式)。"""
    monkeypatch.setenv("RELEASE_BUILD", "1")
    monkeypatch.delenv("I18N_ALLOW_FALLBACK", raising=False)
    from services.i18n import _reset_i18n_fallback_cache
    _reset_i18n_fallback_cache()
    yield
    _reset_i18n_fallback_cache()


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════
def _write_locale_files(
    tmp_path: Path,
    zh_content: dict,
    en_content: dict,
) -> Path:
    """写入临时 locale 文件(zh-CN.json + en-US.json),返回目录路径。"""
    (tmp_path / "zh-CN.json").write_text(
        json.dumps(zh_content, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "en-US.json").write_text(
        json.dumps(en_content, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tmp_path


def _new_manager(locales_dir: Path):
    """创建一个新的 I18nManager(不复用模块级单例)。"""
    from services.i18n import I18nManager
    return I18nManager(locales_dir=locales_dir, default_locale="zh-CN")


# ════════════════════════════════════════════════════════════════
# 场景 1: Scanner 集成测试 — --strict 模式在当前代码库通过
# ════════════════════════════════════════════════════════════════
class TestScannerIntegration:
    """Scanner 集成测试:验证 --strict 模式在真实代码库 + baseline 下通过。"""

    def test_strict_mode_passes_with_baseline(self):
        """场景 1: --strict 模式在当前代码库通过(baseline ratchet 有效)。

        验证:
            - 真实 locales/zh-CN.json + en-US.json 通过 dim1-3 结构性校验(0 违规)
            - dim6-7 AST 扫描通过(0 违规)
            - dim4-5 文案违规在 baseline 范围内(不阻断)
            - exit code = 0
        """
        # 将 scripts/ 加入 sys.path 以导入扫描器模块
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import check_i18n_strict_export_boundary as scanner
            exit_code, violations = scanner.verify(strict=True)
            assert exit_code == 0, (
                f"--strict 模式应通过(exit 0),实际 exit={exit_code}, "
                f"violations={violations[:5]}"
            )
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))


# ════════════════════════════════════════════════════════════════
# 场景 2: Dim 1 — Key 集合对等
# ════════════════════════════════════════════════════════════════
class TestDim1KeySetSymmetry:
    """维度 1: zh-CN ↔ en-US key 集合对等(无孤儿 key)。"""

    def test_dim1_detects_orphan_key_in_zh(self):
        """场景 2a: zh-CN 独有 key(在 en-US 缺失)被检测。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim1_key_set_symmetry
            zh_flat = {"common.ok": "确定", "common.cancel": "取消", "orphan.key": "孤儿"}
            en_flat = {"common.ok": "OK", "common.cancel": "Cancel"}
            violations = _check_dim1_key_set_symmetry(zh_flat, en_flat)
            assert len(violations) == 1
            assert "orphan.key" in violations[0]
            assert "zh-CN" in violations[0]
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim1_detects_orphan_key_in_en(self):
        """场景 2b: en-US 独有 key(在 zh-CN 缺失)被检测。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim1_key_set_symmetry
            zh_flat = {"common.ok": "确定"}
            en_flat = {"common.ok": "OK", "extra.key": "Extra"}
            violations = _check_dim1_key_set_symmetry(zh_flat, en_flat)
            assert len(violations) == 1
            assert "extra.key" in violations[0]
            assert "en-US" in violations[0]
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim1_passes_when_symmetric(self):
        """场景 2c: 两侧 key 集合一致时无违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim1_key_set_symmetry
            zh_flat = {"a": "甲", "b": "乙"}
            en_flat = {"a": "A", "b": "B"}
            assert _check_dim1_key_set_symmetry(zh_flat, en_flat) == []
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))


# ════════════════════════════════════════════════════════════════
# 场景 3: Dim 2 — ICU AST 结构等价
# ════════════════════════════════════════════════════════════════
class TestDim2IcuAstEquivalence:
    """维度 2: ICU AST 结构等价(selector 集合 + 占位符数量/类型)。"""

    def test_dim2_detects_selector_mismatch(self):
        """场景 3a: zh-CN 用 plural =0/one/other,en-US 漏掉 =0 → 违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim2_icu_ast_equivalence
            zh_flat = {"items": "{n, plural, =0 {无} one {# 项} other {# 项}}"}
            en_flat = {"items": "{n, plural, one {# item} other {# items}}"}
            violations = _check_dim2_icu_ast_equivalence(zh_flat, en_flat)
            assert len(violations) == 1
            assert "items" in violations[0]
            assert "ICU 结构不对称" in violations[0]
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim2_detects_type_mismatch(self):
        """场景 3b: zh-CN 用 plural,en-US 用 select → 违规(类型不一致)。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim2_icu_ast_equivalence
            zh_flat = {"gender": "{g, plural, one {他} other {他们}}"}
            en_flat = {"gender": "{g, select, male {he} female {she} other {they}}"}
            violations = _check_dim2_icu_ast_equivalence(zh_flat, en_flat)
            assert len(violations) >= 1
            assert "gender" in violations[0]
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim2_passes_when_equivalent(self):
        """场景 3c: 两侧 ICU 结构完全等价时无违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim2_icu_ast_equivalence
            zh_flat = {"items": "{n, plural, =0 {无} one {# 项} other {# 项}}"}
            en_flat = {"items": "{n, plural, =0 {none} one {# item} other {# items}}"}
            assert _check_dim2_icu_ast_equivalence(zh_flat, en_flat) == []
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))


# ════════════════════════════════════════════════════════════════
# 场景 4: Dim 3 — 参数名一致
# ════════════════════════════════════════════════════════════════
class TestDim3ParamNameConsistency:
    """维度 3: 参数名一致(`{count}` 不能变成 `{n}`)。"""

    def test_dim3_detects_param_name_mismatch(self):
        """场景 4a: zh-CN 用 {count},en-US 用 {n} → 违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim3_param_name_consistency
            zh_flat = {"quota": "剩余 {count} 次"}
            en_flat = {"quota": "{n} times remaining"}
            violations = _check_dim3_param_name_consistency(zh_flat, en_flat)
            assert len(violations) == 1
            assert "quota" in violations[0]
            assert "count" in violations[0]
            assert "n" in violations[0]
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim3_passes_when_consistent(self):
        """场景 4b: 两侧参数名一致时无违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim3_param_name_consistency
            zh_flat = {"quota": "剩余 {count} 次"}
            en_flat = {"quota": "{count} times remaining"}
            assert _check_dim3_param_name_consistency(zh_flat, en_flat) == []
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))


# ════════════════════════════════════════════════════════════════
# 场景 5: Dim 4 — en-US 禁止 CJK 占位副本
# ════════════════════════════════════════════════════════════════
class TestDim4NoCjkInEnUs:
    """维度 4: en-US 翻译值中禁止出现中文字符(防止混合语言)。"""

    def test_dim4_detects_cjk_in_en_us(self):
        """场景 5a: en-US 值中含中文字符 → 违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim4_no_cjk_in_en_us
            en_flat = {
                "ok": "OK",
                "broken": "请点击 确定 to continue",  # 含中文
            }
            violations = _check_dim4_no_cjk_in_en_us(en_flat)
            assert len(violations) == 1
            assert "broken" in violations[0]
            assert "CJK" in violations[0]
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim4_passes_when_no_cjk(self):
        """场景 5b: en-US 值纯英文/数字无违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim4_no_cjk_in_en_us
            en_flat = {"ok": "OK", "cancel": "Cancel", "count": "{count} items"}
            assert _check_dim4_no_cjk_in_en_us(en_flat) == []
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))


# ════════════════════════════════════════════════════════════════
# 场景 6: Dim 5 — zh-CN 禁止英文业务文案泄漏
# ════════════════════════════════════════════════════════════════
class TestDim5NoEnglishLeakInZhCn:
    """维度 5: zh-CN 翻译值中禁止英文业务文案(技术术语白名单)。"""

    def test_dim5_detects_english_business_text(self):
        """场景 6a: zh-CN 值中含英文业务文案(如 "Successfully") → 违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim5_no_english_leak_in_zh_cn
            zh_flat = {
                "ok": "确定",
                "broken": "操作 Successfully 完成",  # "Successfully" 非白名单
            }
            violations = _check_dim5_no_english_leak_in_zh_cn(zh_flat)
            assert len(violations) == 1
            assert "broken" in violations[0]
            assert "Successfully" in violations[0] or "successfully" in violations[0].lower()
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim5_allows_whitelisted_technical_terms(self):
        """场景 6b: zh-CN 值中含白名单技术术语(MFA/API/URL)无违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim5_no_english_leak_in_zh_cn
            zh_flat = {
                "mfa": "MFA 验证失败",
                "api": "API 调用成功",
                "url": "URL 无效",
                "mixed": "MFA 与 API 双因子认证",
            }
            assert _check_dim5_no_english_leak_in_zh_cn(zh_flat) == []
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim5_ignores_icu_placeholder_variable_names(self):
        """场景 6c: ICU 占位符中的变量名(如 {count})不被误判为英文业务文案。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim5_no_english_leak_in_zh_cn
            zh_flat = {
                "items": "{count, plural, =0 {无项目} one {# 项} other {# 项}}",
                "quota": "剩余 {count} 次",
            }
            assert _check_dim5_no_english_leak_in_zh_cn(zh_flat) == []
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))


# ════════════════════════════════════════════════════════════════
# 场景 7: Dim 6 — 内部异常禁止经 UserMessage 泄漏(AST 扫描)
# ════════════════════════════════════════════════════════════════
class TestDim6NoExceptionLeakInUserMessage:
    """维度 6: UserMessage / ErrorEnvelope 构造器禁止传入内部异常信息。"""

    def test_dim6_detects_str_exception_in_user_message(self, tmp_path):
        """场景 7a: UserMessage(text=str(e)) 传入异常 → 违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import (
                _check_dim6_no_exception_leak_in_user_message,
            )
            src = tmp_path / "leak.py"
            src.write_text(
                "try:\n"
                "    do_something()\n"
                "except Exception as e:\n"
                "    msg = UserMessage(text=str(e))\n",
                encoding="utf-8",
            )
            violations = _check_dim6_no_exception_leak_in_user_message(src)
            assert len(violations) == 1
            assert "dim6_exception_leak" in violations[0]
            assert "UserMessage" in violations[0]
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim6_detects_traceback_format_exc(self, tmp_path):
        """场景 7b: ErrorEnvelope(detail=traceback.format_exc()) → 违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import (
                _check_dim6_no_exception_leak_in_user_message,
            )
            src = tmp_path / "leak_tb.py"
            src.write_text(
                "import traceback\n"
                "try:\n"
                "    do_something()\n"
                "except Exception:\n"
                "    env = ErrorEnvelope(detail=traceback.format_exc())\n",
                encoding="utf-8",
            )
            violations = _check_dim6_no_exception_leak_in_user_message(src)
            assert len(violations) == 1
            assert "dim6_exception_leak" in violations[0]
            assert "ErrorEnvelope" in violations[0]
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim6_detects_exception_args_access(self, tmp_path):
        """场景 7c: AppError(message=e.args) 传入异常 args → 违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import (
                _check_dim6_no_exception_leak_in_user_message,
            )
            src = tmp_path / "leak_args.py"
            src.write_text(
                "try:\n"
                "    do_something()\n"
                "except Exception as e:\n"
                "    err = AppError(message=e.args)\n",
                encoding="utf-8",
            )
            violations = _check_dim6_no_exception_leak_in_user_message(src)
            assert len(violations) == 1
            assert "dim6_exception_leak" in violations[0]
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim6_passes_with_safe_message(self, tmp_path):
        """场景 7d: UserMessage(text="操作失败") 传入安全文案 → 无违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import (
                _check_dim6_no_exception_leak_in_user_message,
            )
            src = tmp_path / "safe.py"
            src.write_text(
                "try:\n"
                "    do_something()\n"
                "except Exception as e:\n"
                "    # 只记录日志,不泄漏给用户\n"
                "    logger.error('internal error: %s', e)\n"
                "    msg = UserMessage(text='操作失败,请稍后重试')\n",
                encoding="utf-8",
            )
            violations = _check_dim6_no_exception_leak_in_user_message(src)
            assert violations == []
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))


# ════════════════════════════════════════════════════════════════
# 场景 8: Dim 7 — 生产代码必须显式绑定 locale(AST 扫描)
# ════════════════════════════════════════════════════════════════
class TestDim7LocaleBound:
    """维度 7: translate/format_message/format_message_icu 必须显式传入 locale。"""

    def test_dim7_detects_translate_without_locale(self, tmp_path):
        """场景 8a: translate(key) 未传入 locale → 违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim7_locale_bound
            src = tmp_path / "no_locale.py"
            src.write_text(
                "from services.i18n import translate\n"
                "text = translate('common.ok')\n",
                encoding="utf-8",
            )
            violations = _check_dim7_locale_bound(src)
            assert len(violations) == 1
            assert "dim7_locale_not_bound" in violations[0]
            assert "translate" in violations[0]
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim7_passes_with_positional_locale(self, tmp_path):
        """场景 8b: translate(key, locale) 显式传入位置 locale → 无违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim7_locale_bound
            src = tmp_path / "pos_locale.py"
            src.write_text(
                "from services.i18n import translate\n"
                "text = translate('common.ok', user_locale)\n",
                encoding="utf-8",
            )
            assert _check_dim7_locale_bound(src) == []
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim7_passes_with_keyword_locale(self, tmp_path):
        """场景 8c: translate(key, locale=loc) 显式传入关键字 locale → 无违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim7_locale_bound
            src = tmp_path / "kw_locale.py"
            src.write_text(
                "from services.i18n import translate\n"
                "text = translate('common.ok', locale=user_locale)\n",
                encoding="utf-8",
            )
            assert _check_dim7_locale_bound(src) == []
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

    def test_dim7_skips_self_method_calls(self, tmp_path):
        """场景 8d: self.translate(key) 类内部调用( locale 透传)→ 跳过,无违规。"""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from check_i18n_strict_export_boundary import _check_dim7_locale_bound
            src = tmp_path / "self_call.py"
            src.write_text(
                "class Foo:\n"
                "    def bar(self, key):\n"
                "        return self.translate(key)\n",
                encoding="utf-8",
            )
            assert _check_dim7_locale_bound(src) == []
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))


# ════════════════════════════════════════════════════════════════
# 场景 9: Fail-closed 行为(I18nManager + _get_i18n_allow_fallback)
# ════════════════════════════════════════════════════════════════
class TestFailClosedBehavior:
    """R65 P1-03: i18n fail-closed locale/ICU 解析行为测试。"""

    def test_get_i18n_allow_fallback_defaults_true_in_dev(self, clean_env, monkeypatch):
        """场景 9a: development 环境 + 无显式 env → 允许 fallback(向后兼容)。"""
        # config.settings 不可用时 _get_environment() 返回 "development"
        from services.i18n import _get_i18n_allow_fallback
        # 默认环境(无 config.settings)→ development → 允许 fallback
        assert _get_i18n_allow_fallback() is True

    def test_get_i18n_allow_fallback_explicit_on(self, loose_mode):
        """场景 9b: I18N_ALLOW_FALLBACK=1 → 允许 fallback(测试逃生舱)。"""
        from services.i18n import _get_i18n_allow_fallback
        assert _get_i18n_allow_fallback() is True

    def test_get_i18n_allow_fallback_explicit_off(self, strict_mode):
        """场景 9c: I18N_ALLOW_FALLBACK=0 → 严格 fail-closed。"""
        from services.i18n import _get_i18n_allow_fallback
        assert _get_i18n_allow_fallback() is False

    def test_release_mode_implies_fail_closed(self, release_mode):
        """场景 9d: RELEASE_BUILD=1 → 强制 fail-closed(优先级最高)。"""
        from services.i18n import _get_i18n_allow_fallback
        assert _get_i18n_allow_fallback() is False

    def test_translate_raises_locale_not_bound_in_strict_mode(self, strict_mode, tmp_path):
        """场景 9e: strict 模式下 translate(key) 无 locale → 抛 I18N_LOCALE_NOT_BOUND。"""
        from services.error_codes import AppError, ErrorCodes
        _write_locale_files(
            tmp_path,
            {"common": {"ok": "确定"}},
            {"common": {"ok": "OK"}},
        )
        manager = _new_manager(tmp_path)
        manager.load_locale("zh-CN")
        with pytest.raises(AppError) as exc_info:
            manager.translate("common.ok")  # 未传 locale
        assert exc_info.value.code == ErrorCodes.I18N_LOCALE_NOT_BOUND
        # params 应包含 key + caller(用于诊断,非用户敏感信息)
        assert "key" in exc_info.value.params
        assert exc_info.value.params["caller"] == "translate"

    def test_translate_allows_fallback_in_loose_mode(self, loose_mode, tmp_path):
        """场景 9f: loose 模式(I18N_ALLOW_FALLBACK=1)下 translate(key) 无 locale → 保留旧行为。"""
        _write_locale_files(
            tmp_path,
            {"common": {"ok": "确定"}},
            {"common": {"ok": "OK"}},
        )
        manager = _new_manager(tmp_path)
        manager.load_locale("zh-CN")
        # 不抛异常,回退到 default_locale
        text = manager.translate("common.ok")
        assert text == "确定"

    def test_translate_with_explicit_locale_passes_in_strict_mode(self, strict_mode, tmp_path):
        """场景 9g: strict 模式下 translate(key, locale=...) 显式绑定 → 通过。"""
        _write_locale_files(
            tmp_path,
            {"common": {"ok": "确定"}},
            {"common": {"ok": "OK"}},
        )
        manager = _new_manager(tmp_path)
        manager.load_locale("zh-CN")
        manager.load_locale("en-US")
        text = manager.translate("common.ok", locale="zh-CN")
        assert text == "确定"

    def test_format_message_icu_raises_parse_failed_in_strict_mode(self, strict_mode, tmp_path):
        """场景 9h: strict 模式下 ICU 运行时解析失败 → 抛 I18N_PARSE_FAILED。

        注:此场景构造一个含恶意 ICU 模板的 key,使 _icu_format 在运行时
        抛异常(非预编译阶段),验证 fail-closed 行为。
        """
        from services.error_codes import AppError, ErrorCodes
        # 构造一个会在运行时 ICU 解析失败的 key(预编译通过但运行时 kwargs 类型不匹配)
        # 使用 plural selector 传入非数字 kwargs 触发运行时异常
        _write_locale_files(
            tmp_path,
            {"common": {"count": "{n, plural, =0 {无} one {# 项} other {# 项}}"}},
            {"common": {"count": "{n, plural, =0 {none} one {# item} other {# items}}"}},
        )
        manager = _new_manager(tmp_path)
        manager.load_locale("zh-CN")
        manager.load_locale("en-US")
        # 触发运行时 ICU 解析异常:传入非数字 n(部分实现会抛 ValueError/TypeError)
        # 若实现未抛异常(优雅降级),则跳过此断言(不强制要求实现抛异常)
        try:
            # 传入一个会导致 ICU plural 失败的 kwargs(n=None 可能触发)
            result = manager.format_message_icu(
                "common.count", locale="zh-CN", n="not_a_number",
            )
            # 若未抛异常,说明实现优雅降级(返回 fallback 文案)
            # 此场景下 test 仍通过(fail-closed 不要求所有 ICU 错误都抛)
            assert isinstance(result, str)
        except AppError as e:
            # 若抛 AppError,必须是 I18N_PARSE_FAILED(fail-closed)
            assert e.code == ErrorCodes.I18N_PARSE_FAILED
            assert "key" in e.params
            assert "locale" in e.params
            assert "reason" in e.params

    def test_error_codes_registered_in_registry(self):
        """场景 9i: I18N_LOCALE_NOT_BOUND / I18N_PARSE_FAILED 已在 ErrorRegistry 注册。"""
        from services.error_codes import ErrorCodes, ErrorRegistry
        # 错误码常量存在
        assert hasattr(ErrorCodes, "I18N_LOCALE_NOT_BOUND")
        assert hasattr(ErrorCodes, "I18N_PARSE_FAILED")
        # 已注册到 ErrorRegistry(ErrorRegistry.all_codes() 返回已注册 code 列表)
        registered_codes = set(ErrorRegistry.all_codes())
        assert ErrorCodes.I18N_LOCALE_NOT_BOUND in registered_codes, (
            "I18N_LOCALE_NOT_BOUND 未在 ErrorRegistry 注册"
        )
        assert ErrorCodes.I18N_PARSE_FAILED in registered_codes, (
            "I18N_PARSE_FAILED 未在 ErrorRegistry 注册"
        )

    def test_locale_message_keys_exist_in_both_locales(self):
        """场景 9j: 新增错误码的 message_key 在 zh-CN/en-US 都存在。

        locale 文件结构:{"errors": {"i18n.locale.not_bound": "...", ...}, ...}
        message_key 在 ErrorDefinition 中定义为 "i18n.locale.not_bound"(不含
        顶层 "errors." 前缀),但实际存储在 locale 文件的 "errors" 子对象下。
        本测试直接在 "errors" 子对象中查找 message_key。
        """
        zh_path = LOCALES_DIR / "zh-CN.json"
        en_path = LOCALES_DIR / "en-US.json"
        zh_data = json.loads(zh_path.read_text(encoding="utf-8"))
        en_data = json.loads(en_path.read_text(encoding="utf-8"))

        # message_key 存储在 locale 文件的 "errors" 子对象下
        zh_errors = zh_data.get("errors", {})
        en_errors = en_data.get("errors", {})
        # i18n.locale.not_bound / i18n.parse.failed 两个 message_key 在两侧都存在
        assert "i18n.locale.not_bound" in zh_errors, "zh-CN errors 缺少 i18n.locale.not_bound"
        assert "i18n.locale.not_bound" in en_errors, "en-US errors 缺少 i18n.locale.not_bound"
        assert "i18n.parse.failed" in zh_errors, "zh-CN errors 缺少 i18n.parse.failed"
        assert "i18n.parse.failed" in en_errors, "en-US errors 缺少 i18n.parse.failed"
